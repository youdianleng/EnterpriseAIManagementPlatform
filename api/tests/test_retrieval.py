"""Hybrid retrieval, reciprocal-rank fusion, the rerank and the "no basis" state.

Real PostgreSQL with real pgvector, real chunks written by ticket 32's pipeline, and
the real GIN index. The embedding *transport* is the only double — ticket 32's
`DeterministicEmbedder`, which is a second adapter rather than a mock — for the reason
`tests/test_embeddings.py` gives: the vectors have to be reproducible for a ranking
assertion to mean anything, and the development stack runs without a key.

Every test names the checklist line it pins. The four that carry the ticket:

* `test_rrf_scores_are_the_sum_of_the_legs_contributions` — **the arithmetic**, with no
  database at all. This is the test that proves the fusion rule; everything else proves
  the SQL that feeds it.
* `test_both_legs_are_one_round_trip` — what "并行执行" means here, asserted with a
  counting session rather than described in a comment.
* `test_the_reranker_seam_is_used` — a fake adapter that records its calls, because "the
  seam exists" and "the seam is reached" are different claims.
* `test_the_threshold_boundary_is_pinned_from_both_sides` — one query a hair above the
  threshold and the same query a hair below, so the "no basis" rule has a boundary
  rather than a direction.

Two tests are the **mutation** targets: `test_rrf_scores_are_the_sum_of_the_legs_
contributions` (the fusion may not ignore a leg) and
`test_a_weak_query_returns_no_basis_rather_than_a_low_scoring_five` (the threshold may
not be skipped).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from uuid import UUID, uuid4

import pytest

from app.domain.access.kernel import ResourceKind, filter_for
from app.domain.access.principal import Principal
from app.domain.document.embeddings import DeterministicEmbedder
from app.domain.retrieval.filtering import retrieval_filter_explanation, unfiltered
from app.domain.retrieval.models import (
    FusedCandidate,
    RankedCandidate,
    RankedHit,
    RetrievalLeg,
    RrfLeg,
)
from app.domain.retrieval.rerank import LexicalReranker, score_of, terms_of
from app.domain.retrieval.service import RetrievalService
from app.repositories.retrieval import (
    PostgresChunkSearchRepository,
    visible_document_clauses,
    visible_document_predicate,
)
from tests.support.platform import Actor, Platform
from tests.support.retrieval_sample import DOCUMENTS, QUESTIONS
from tests.test_documents import Cast, run_parse

#: What the fusion constant is in every assertion, written out rather than read from
#: settings: a test that imported the value would agree with a settings change instead
#: of catching it. 60 is the literature's value and the default.
K = 60


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def document_storage(tmp_path, monkeypatch) -> str:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "document_storage_path", str(tmp_path))
    return str(tmp_path)


@dataclass(frozen=True, slots=True)
class Corpus:
    """The sample corpus as a fixture: `title → id`, and the caller that uploaded it.

    **The actor is part of the corpus because the corpus is a company knowledge base.**
    §4.2 reaches one of those through a department, so a caller who works in that
    department reads it and a caller who does not reads it through *no* clause — which is
    the correct rule, and the reason "search the corpus and expect an answer" is a
    statement only one principal in this module can make. The administrator below is that
    principal: it is assigned to the document's own department and holds `admin`, so it is
    the same caller an operator would use to look at the knowledge base by hand.
    """

    ids: dict[str, str]
    admin: Actor

async def upload_document(
    platform: Platform,
    actor: Actor,
    document,  # noqa: ANN001 - a `SampleDocument`
    *,
    department: str,
    clearance: str = "low",
    is_company_kb: bool = True,
) -> str:
    """One document through the real upload endpoint and the real parse, and its id.

    Factored out of `index` because the escalation suite needs the same thing with the
    two fields `index` fixes: *which* department the document is filed into and at what
    clearance. A second copy of this in that module would be the third place an upload
    fixture lives, and the one most likely to drift from the pipeline it is pretending
    to be.

    `actor` must be able to file into `department` at `clearance`: the service refuses an
    upload above its own author's ceiling, and a company document filed by a role whose
    remit is the knowledge base. Both are `test_documents.py`'s subject; here they are a
    precondition the caller sets up.
    """
    from tests.support.documents import markdown_bytes

    response = await actor.post(
        "/api/v1/documents",
        files={
            "file": (
                document.filename,
                markdown_bytes(document.body),
                "application/octet-stream",
            )
        },
        data={
            "title": document.title,
            "is_company_kb": "true" if is_company_kb else "false",
            "department_id": department,
            "clearance_level": clearance,
        },
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["id"]
    assert await run_parse(platform, document_id, embedder=DeterministicEmbedder())
    return document_id


async def index(platform: Platform, cast: Cast, *documents) -> Corpus:  # noqa: ANN001
    """Upload and parse the sample corpus, and answer with it.

    Through the real endpoints and the real pipeline: an upload, then `run_parse` the
    way the job drives it. A fixture that inserted chunk rows by hand would exercise the
    retrieval SQL over text the splitter never produced — including `search_vector`,
    which is generated and would therefore be *right* while the pages and the parent
    links were invented.
    """
    admin = await platform.account(roles=("admin",))
    await platform.assign(
        admin.employee_id, cast.department, await platform.position(cast.department, "kbase")
    )
    ids: dict[str, str] = {}
    for document in documents:
        ids[document.title] = await upload_document(
            platform, admin, document, department=cast.department
        )
    return Corpus(ids=ids, admin=admin)


@asynccontextmanager
async def service(
    platform: Platform, *, embedding: bool = True, **overrides
) -> AsyncIterator[RetrievalService]:  # noqa: ANN003
    """A real service on the test database, over a session this closes on the way out.

    Built by hand rather than through the router because most of these tests assert the
    module's own contract — a `FilterSpec` the kernel produced, a reranker that records,
    a threshold moved to a boundary — and only the HTTP tests go through the endpoint.

    **The transaction is rolled back and the session closed, and that is not tidiness.**
    A retrieval runs a `SELECT` inside an implicit transaction, so a session left open
    sits in `idle in transaction` holding a read lock on every table it touched — and the
    fixture that ends the next test begins with `TRUNCATE ... CASCADE`, which then waits
    for it. One leaked session does not fail a test; it hangs the suite, which is how this
    was found (the truncate was still waiting ten minutes later).
    """
    session = platform.factory()
    try:
        yield RetrievalService(
            PostgresChunkSearchRepository(session),
            embedder=DeterministicEmbedder() if embedding else None,
            **overrides,
        )
    finally:
        await session.rollback()
        await session.close()


@dataclass
class RecordingReranker:
    """A `Reranker` that records what it was asked, and answers with the fusion order.

    The double exists for one claim — that the service *calls* the seam — for the
    reason `RecordingEmbedder` does: "the seam exists" is a fact about a class, and
    "the seam is reached" is a fact about calls. It returns `RankedHit`s rather than
    being a `Mock`, so the result it produces is a value the service can go on using.
    """

    name: str = "recording-reranker"
    calls: list[tuple[str, int]] = field(default_factory=list)

    def rerank(self, query: str, candidates) -> list[RankedHit]:  # noqa: ANN001
        self.calls.append((query, len(candidates)))
        terms = terms_of(query)
        return [
            RankedHit(fused=item, breakdown=score_of(terms, item)) for item in candidates
        ]


# --- part 1: the fusion arithmetic, without a database -----------------------


def candidate(
    rank: int,
    *,
    chunk_id: UUID | None = None,
    name: str = "chunk",
    content: str = "",
    parent_content: str | None = None,
) -> RankedCandidate:
    """One leg's row, with everything the fusion reads and nothing it does not."""
    return RankedCandidate(
        chunk_id=chunk_id or uuid4(),
        document_id=uuid4(),
        document_title="Politica",
        filename="politica.md",
        content=content or f"the text of {name}",
        parent_chunk_id=uuid4() if parent_content is not None else None,
        parent_content=parent_content,
        heading_path=None,
        page_from=1,
        page_to=1,
        rank=rank,
        vector_distance=None,
        text_rank=None,
    )


def test_rrf_scores_are_the_sum_of_the_legs_contributions() -> None:
    """**Mutation test**: the fusion may not ignore a leg. The ticket's arithmetic.

    `1 / (k + rank)` per leg, summed, ordered descending. The three assertions are three
    different mistakes:

    * the *value* catches a wrong constant, a wrong rank or a missing leg;
    * the *ordering* catches a fusion that sums correctly and sorts backwards, or that
      lets one leg's first place beat a candidate both legs agreed on — which is the
      whole reason the constant damps the top;
    * the *contribution list* catches a fusion that computes the score from one leg and
      reports the other, which would make every debug view a lie.

    The legs are passed as mappings keyed by chunk id, so "which leg is this rank from"
    is the dictionary's name and not the argument's position — a fusion that read the
    wrong leg's rank is not expressible through this signature.
    """
    from app.domain.retrieval.fusion import by_chunk, reciprocal_rank_fusion

    both = candidate(1, content="found by both legs")
    vector_only = candidate(2, content="found by the vector leg alone")
    text_only = candidate(1, content="found by the text leg alone")
    # The same chunk as `both`, second in the text leg — which is what makes this
    # candidate's score a *sum* rather than one leg's term twice.
    both_in_text = replace(both, rank=2)

    fused = reciprocal_rank_fusion(
        by_chunk([both, vector_only]), by_chunk([text_only, both_in_text]), k=K
    )

    by_content = {item.candidate.content: item for item in fused}
    agree = by_content["found by both legs"]
    # First in the vector leg, second in the text leg: 1/61 + 1/62.
    assert agree.fusion_score == pytest.approx(1 / 61 + 1 / 62)
    assert [entry.leg for entry in agree.legs] == [RetrievalLeg.VECTOR, RetrievalLeg.TEXT]
    assert [entry.rank for entry in agree.legs] == [1, 2]
    assert agree.legs[0].contribution == pytest.approx(1 / 61)
    assert agree.legs[1].contribution == pytest.approx(1 / 62)
    assert agree.from_legs is RetrievalLeg.BOTH
    assert agree.rank_in(RetrievalLeg.VECTOR) == 1
    assert agree.rank_in(RetrievalLeg.TEXT) == 2
    assert agree.contribution_of(RetrievalLeg.TEXT) == pytest.approx(1 / 62)

    # A candidate one leg found can never outrank one both legs found: 1/62 alone is
    # less than 1/61 + 1/62, and that is the property the constant exists to give.
    assert fused[0].candidate.content == "found by both legs"
    assert by_content["found by the vector leg alone"].from_legs is RetrievalLeg.VECTOR
    assert by_content["found by the text leg alone"].from_legs is RetrievalLeg.TEXT

    # Every contribution is present, so the debug view's sum is checkable against the
    # score it reports.
    for item in fused:
        assert item.fusion_score == pytest.approx(
            sum(entry.contribution for entry in item.legs)
        )


def test_the_text_legs_ranks_change_the_fusion() -> None:
    """**Mutation test (a)**: make the fusion ignore the text leg and this fails.

    The verification the ticket asks for. A "vector-only fusion" — one that returns the
    vector leg's list with the text leg's contributions dropped — keeps every score
    ordering *within* the vector leg, so a weaker test would pass. This one pins the two
    facts that cannot survive it: a candidate **only** the text leg found is missing
    entirely, and a candidate both legs found loses the text leg's term.
    """
    from app.domain.retrieval.fusion import by_chunk, reciprocal_rank_fusion

    only_text = candidate(1, content="only the text leg found this")
    in_both = candidate(1, content="both legs found this")
    vector_tail = candidate(2, content="vector tail")
    in_both_in_text = replace(in_both, rank=2)

    fused = reciprocal_rank_fusion(
        by_chunk([in_both, vector_tail]), by_chunk([only_text, in_both_in_text]), k=K
    )
    ids = [item.candidate.chunk_id for item in fused]

    assert only_text.chunk_id in ids, "the text leg's own top hit vanished from the fusion"
    assert len(fused) == 3
    scores = {item.candidate.content: item.fusion_score for item in fused}
    assert scores["only the text leg found this"] == pytest.approx(1 / 61)
    assert scores["both legs found this"] == pytest.approx(1 / 61 + 1 / 62)
    # With the text leg's ranks ignored, the second candidate would score 1/61 too — and
    # the first would be absent — which is exactly what the mutation produces.
    assert scores["both legs found this"] > scores["only the text leg found this"]


def test_a_single_leg_fuses_to_itself() -> None:
    """Both legs are consulted, and an empty one is ordinary rather than a special case.

    A deployment with `EMBEDDING_PROVIDER=none` runs the text leg alone on every query,
    and a query that shares no stem with the corpus runs the vector leg alone. A fusion
    that took "the first non-empty mapping" would pass this test by accident and fuse
    nothing the moment both answered.
    """
    from app.domain.retrieval.fusion import by_chunk, reciprocal_rank_fusion

    rows = [candidate(1), candidate(2), candidate(3)]

    text_only = reciprocal_rank_fusion({}, by_chunk(rows), k=K)
    assert [item.fusion_score for item in text_only] == pytest.approx(
        [1 / 61, 1 / 62, 1 / 63]
    )
    assert all(item.from_legs is RetrievalLeg.TEXT for item in text_only)

    vector_only = reciprocal_rank_fusion(by_chunk(rows), {}, k=K)
    assert [item.candidate.chunk_id for item in vector_only] == [
        item.chunk_id for item in rows
    ]
    assert all(item.from_legs is RetrievalLeg.VECTOR for item in vector_only)

    assert reciprocal_rank_fusion({}, {}, k=K) == []


def test_the_fusion_constant_scales_the_ranks_it_is_given() -> None:
    """`k` is a setting, and it is *not* decoration: the scores move with it.

    Three candidates: two the vector leg found, of which the text leg also found both — in
    the opposite order — and one only the text leg found. Every score is therefore a sum
    of two contributions, which is what makes the value assertions meaningful rather than
    a restatement of one leg.

    At `k = 0` the contributions are `1/rank` exactly, which is where the constant is
    checkable by hand; at `k = 60` the same candidates are `1/6x` each. What `k` changes
    is the *margin* — how much a shared place is worth over a single one — and a test
    that only asserted "the order is unchanged" would pass for a fusion that ignored `k`
    entirely, so the margin is what is asserted.
    """
    from app.domain.retrieval.fusion import by_chunk, reciprocal_rank_fusion

    # Three dimensions, not three chunks of one document: two candidates that agree on
    # every tiebreaker are the same candidate, and the fusion would be right to merge
    # them.
    leader = candidate(1, content="first in the vector leg")
    runner_up = replace(leader, chunk_id=uuid4(), document_id=uuid4(), rank=2)
    only_text_leg = replace(
        leader, chunk_id=uuid4(), document_id=uuid4(), rank=4, content="only the text leg"
    )
    # The text leg's own view: the runner-up first, the leader second, and the
    # text-only candidate third — so the two orders disagree, which is what makes the
    # margin visible.
    text_leg = by_chunk(
        [
            replace(runner_up, rank=1),
            replace(leader, rank=2),
            replace(only_text_leg, rank=3),
        ]
    )
    vector_leg = by_chunk([leader, runner_up])

    damped = reciprocal_rank_fusion(vector_leg, text_leg, k=K)
    undamped = reciprocal_rank_fusion(vector_leg, text_leg, k=0)

    order = [item.candidate.chunk_id for item in damped]
    assert order[2] == only_text_leg.chunk_id, f"the text-only candidate was not last: {order}"
    assert set(order[:2]) == {leader.chunk_id, runner_up.chunk_id}, (
        f"the two candidates both legs found did not come first: {order}"
    )
    assert [item.candidate.chunk_id for item in undamped] == order
    # Hand-checkable at `k = 0`, where a contribution is exactly `1/rank`:
    #   leader      first in the vector leg, second in the text leg → 1 + 1/2
    #   runner-up   first in the text leg, second in the vector leg → 1 + 1/2
    #   text-only   third in the text leg, absent from the vector  → 1/3
    assert undamped[0].fusion_score == pytest.approx(1.5)
    assert undamped[1].fusion_score == pytest.approx(1.5)
    assert undamped[2].fusion_score == pytest.approx(1 / 3)
    # And at `k = 60` the same two sums with every rank damped by the constant.
    assert damped[0].fusion_score == pytest.approx(1 / 61 + 1 / 62)
    assert damped[1].fusion_score == pytest.approx(1 / 61 + 1 / 62)
    assert damped[2].fusion_score == pytest.approx(1 / 63)
    # The margin is what `k` buys, and it is why the constant exists: at `k = 0` the
    # shared candidates are worth four and a half times the single-leg one, and at
    # `k = 60` only about two. A test that asserted merely "the order is unchanged"
    # would pass for a fusion that ignored `k` entirely.
    assert (damped[0].fusion_score / damped[2].fusion_score) < (
        undamped[0].fusion_score / undamped[2].fusion_score
    )


def test_the_fusion_refuses_a_negative_constant() -> None:
    """`1 / (k + rank)` with a negative `k` reaching a rank is a division by zero or a
    negative score — a ranking nothing downstream can threshold. Refused at the door."""
    from app.domain.retrieval.fusion import reciprocal_rank_fusion

    with pytest.raises(ValueError):
        reciprocal_rank_fusion({}, {}, k=-1)


def test_the_reranker_prefers_a_passage_that_answers_the_question() -> None:
    """The lexical reranker's own arithmetic, without a database.

    Three candidates in the fusion's own order, and the reranker has to move the two
    whose passages answer the question above the one that answers nothing — including a
    passage the fusion had placed *last*, which is the whole reason a rerank stage exists
    after the fusion. The components are asserted individually because that is what the
    debug view prints, and because "the reranker moved it" is only defensible while each
    part of the score means what its name says.
    """
    query = "vacaciones anuales retribuidas"
    weak = candidate(1, content="contenido sin relación", parent_content="Nada que ver.")
    strong = candidate(
        4,
        content="child matched",
        parent_content="Las vacaciones anuales retribuidas se solicitan por escrito.",
    )
    headed = replace(strong, chunk_id=uuid4(), heading_path="1. Vacaciones anuales")

    fused = [
        FusedCandidate(
            candidate=weak, fusion_score=1 / 61, legs=(RrfLeg(RetrievalLeg.VECTOR, 1, 1 / 61),)
        ),
        FusedCandidate(
            candidate=strong, fusion_score=1 / 64, legs=(RrfLeg(RetrievalLeg.VECTOR, 4, 1 / 64),)
        ),
        FusedCandidate(
            candidate=headed,
            fusion_score=1 / 65,
            legs=(RrfLeg(RetrievalLeg.VECTOR, 5, 1 / 65),),
        ),
    ]

    ranked = LexicalReranker().rerank(query, fused)

    # Both answering passages end up above the unrelated one, including the one the
    # fusion had placed last — which is the whole point of a rerank stage after the
    # fusion, and the reason the fusion's prior is a weight and not the score.
    order = [row.candidate.chunk_id for row in ranked]
    assert order[-1] == weak.chunk_id, f"the unrelated passage was not last: {order}"
    assert set(order[:2]) == {strong.chunk_id, headed.chunk_id}, (
        f"the two answering passages did not both come first: {order}"
    )
    without = ranked[order.index(strong.chunk_id)]
    headed_row = ranked[order.index(headed.chunk_id)]
    assert without.breakdown.term_coverage == 1.0
    assert without.breakdown.proximity > 0.0
    assert ranked[-1].breakdown.term_coverage == 0.0

    # A matching heading is worth something on its own: the *same* passage with the
    # heading the question names scores higher than without it, and the fusion had placed
    # it a rank lower as well.
    assert headed_row.breakdown.heading_match > without.breakdown.heading_match
    assert headed_row.score > without.score, (
        "a matching heading did not raise the score: "
        f"{headed_row.breakdown} vs {without.breakdown}"
    )
    assert without.score > ranked[-1].score
    assert all(0.0 <= hit.score <= 1.0 for hit in ranked)


def test_the_reranker_cannot_do_what_a_cross_encoder_can() -> None:
    """**What it cannot do, as a test rather than as a disclaimer.**

    The question names a thing the passage words differently — "asueto" against
    "vacaciones", "corresponden" against "corresponden" — so the passage that *answers*
    it shares one word with it and no meaning. That one word is all the lexical
    components have: `dias` gives a coverage of a quarter and a proximity of zero (its
    partner is not there), the heading is empty, and the fusion score's prior is
    everything else.

    That is the honest limit of an offline reranker, and it is asserted because the
    alternative is a comment claiming a capability the code does not have. A real
    cross-encoder is the fix, and it is what `Reranker` is a seam for.
    """
    query = "¿Cuántos días de asueto me corresponden?"
    paraphrase = candidate(1, content="veintitrés días laborables de vacaciones anuales")
    unrelated = candidate(2, content="el límite de alojamiento es de 90 euros por noche")

    fused = [
        FusedCandidate(candidate=paraphrase, fusion_score=1 / 61, legs=()),
        FusedCandidate(candidate=unrelated, fusion_score=1 / 62, legs=()),
    ]
    ranked = LexicalReranker().rerank(query, fused)

    by_chunk = {row.candidate.chunk_id: row for row in ranked}
    answered = by_chunk[paraphrase.chunk_id]
    other = by_chunk[unrelated.chunk_id]
    assert other.breakdown.term_coverage == 0.0
    assert answered.breakdown.term_coverage == 0.25, (
        "only `dias` survives the folding and the length cut; anything more would mean "
        "the reranker read the paraphrase"
    )
    assert answered.breakdown.proximity == 0.0, (
        "one term in a passage cannot be proximate to anything"
    )
    assert answered.breakdown.heading_match == 0.0
    # The only thing separating them is the prior — and the prior is the *retrieval's*
    # opinion, which is the point: this stage did not understand the question.
    assert answered.score - other.score == pytest.approx(
        0.35 * (answered.breakdown.prior - other.breakdown.prior)
        + 0.30 * (answered.breakdown.term_coverage - other.breakdown.term_coverage)
    )

    # And the ranking is still the fusion's: the passage the retrieval put first is the
    # one this stage puts first, because nothing it can measure separates them.
    assert [row.candidate.chunk_id for row in ranked] == [
        paraphrase.chunk_id,
        unrelated.chunk_id,
    ]


def test_the_filter_is_a_disjunction_of_clauses() -> None:
    """**§4.2 is four alternatives**, and joining them with `AND` is a different rule.

    This is the mistake the end-to-end filter test caught: nobody's own document is also
    a company document owned by them, so an `AND` filter reaches *nothing* — which looks
    like a working permission boundary and is in fact a search that can never answer.
    The rendering is asserted here, without a database, because the two operators differ
    by one character and by the whole meaning of the rule.
    """
    one_employee = uuid4()
    principal = _principal(employee_id=one_employee, departments=frozenset({uuid4()}))
    spec = filter_for(principal, ResourceKind.DOCUMENT)

    predicate, parameters = visible_document_predicate(spec)

    assert " OR " in predicate, f"the clauses are not alternatives: {predicate}"
    # §4.2's four clauses, wrapped so that the one `OR` between them is the whole
    # operator: the `AND`s that survive are inside the company clause alone.
    assert predicate.startswith("(("), predicate
    assert predicate.count(" OR ") == 1, f"more than one alternative: {predicate}"
    assert predicate.count("(") == predicate.count(")")
    assert parameters["filter_employee_id"] == one_employee
    assert parameters["filter_departments"]

    # A spec that reaches nothing through the company clause renders a `false` term
    # rather than dropping the clause: §4.2 is a disjunction, so the *absence* of an
    # alternative narrows nothing while a `false` term keeps the alternatives honest.
    empty = filter_for(
        _principal(clearance="low", departments=frozenset()), ResourceKind.DOCUMENT
    )
    predicate, _ = visible_document_predicate(empty)
    assert "OR (false)" in predicate, predicate
    assert "is_company_kb" not in predicate, (
        "a caller with no reachable department was given a company clause: " + predicate
    )


# --- part 2: the filter entry point, without a database ---------------------


def test_no_filter_spec_means_no_clause_and_a_spec_means_one() -> None:
    """`filter_spec=None` is *unfiltered*, and that is a different thing from "no reach".

    The ticket's line 「本工单只保证接口留出了过滤入口」 in its two halves: the entry
    point exists, and it does not invent a rule. A spec whose owner and whose
    departments reach nothing renders as `false` — the caller reaches no document — while
    `None` renders as no clause at all. Rendering those two the same way is how a
    filter-free query gets written.
    """
    assert visible_document_clauses(None) == ([], {})

    principal = _principal(employee_id=uuid4(), departments=frozenset())
    clauses, parameters = visible_document_clauses(filter_for(principal, ResourceKind.DOCUMENT))

    assert clauses, "a document spec produced no clause at all"
    assert parameters["filter_employee_id"] == principal.employee_id
    assert any("d.owner_employee_id" in clause for clause in clauses)


def _principal(
    *,
    employee_id: UUID | None = None,
    departments: frozenset[UUID] | None = None,
    clearance: str = "high",
    roles: frozenset[str] = frozenset({"employee"}),
) -> Principal:
    return Principal(
        user_id=uuid4(),
        employee_id=employee_id or uuid4(),
        username="ana",
        roles=roles,
        clearance_level=clearance,
        department_ids=departments if departments is not None else frozenset({uuid4()}),
        primary_department_id=None,
        is_manager=False,
        reports_employee_ids=frozenset(),
    )


# --- part 3: the pipeline, against real PostgreSQL ---------------------------


async def test_both_legs_are_one_round_trip(platform: Platform, cast: Cast) -> None:
    """**"并行" in this implementation**: one statement, two CTE legs, one snapshot.

    A session that counts its own `execute` calls, because "one round trip" is a claim
    about the wire and not about the SQL's text: two statements issued from two
    coroutines on one `AsyncSession` would be two round trips that merely look
    concurrent. The statement's own text is asserted as well — a `WITH ... vector_leg`
    and a `text_leg` in one string — so a refactor that split them is a failing test
    rather than a silent regression in latency and consistency.
    """
    await index(platform, cast, *DOCUMENTS[:1])
    counter = CountingSession(platform)
    counting = counter.session()
    retrieval = RetrievalService(
        PostgresChunkSearchRepository(counting),  # type: ignore[arg-type]
        embedder=DeterministicEmbedder(),
    )
    try:
        outcome = await retrieval.search("permiso matrimonio quince días naturales")
    finally:
        await counting.aclose()

    assert counter.executes == 1, f"the two legs took {counter.executes} round trips"
    assert counter.legs_in_one_statement, (
        "the legs were issued as separate statements: "
        f"{[statement[:60] for statement in counter.statements]}"
    )
    assert outcome.legs_used == (RetrievalLeg.VECTOR, RetrievalLeg.TEXT)
    assert outcome.vector_candidates and outcome.text_candidates


class CountingSession:
    """A session proxy that records `execute` and hands the rest to the real one.

    A proxy rather than a mock, so the statement actually runs: the claim being made is
    "one round trip *that answered*", and a mock would let a test pass on SQL that does
    not parse.
    """

    def __init__(self, platform: Platform) -> None:
        self._platform = platform
        self.executes = 0
        self.statements: list[str] = []

    @property
    def legs_in_one_statement(self) -> bool:
        """Both legs, in one statement of the ones that ran."""
        return any(
            "vector_leg" in statement and "text_leg" in statement
            for statement in self.statements
        )

    def session(self):
        return _Counting(self)


class _Counting:
    """The real session, plus a counter. `aclose` closes what it opened."""

    def __init__(self, owner: CountingSession) -> None:
        self._owner = owner
        self._inner = owner._platform.factory()

    async def execute(self, statement, parameters=None):  # noqa: ANN001
        self._owner.executes += 1
        self._owner.statements.append(str(statement))
        return await self._inner.execute(statement, parameters or {})

    async def aclose(self) -> None:
        await self._inner.close()


async def test_every_sample_question_finds_its_document(platform: Platform, cast: Cast) -> None:
    """The sample corpus answers its own questions — asserted, so the fixture and the
    measurement cannot drift apart.

    `tests/support/retrieval_sample.py` is what `tests/tools/eval_retrieval.py` measures
    the three modes on, and a sample whose questions no longer match its documents would
    make every number in the ticket's report a number about nothing. This is also the
    test that would catch a change to the chunker that stopped a section being split
    where the questions expect it.
    """
    await index(platform, cast, *DOCUMENTS)

    misses: list[str] = []
    async with service(platform) as retrieval:
        for question in QUESTIONS:
            outcome = await retrieval.search(question.question)
            titles = {hit.document.title for hit in outcome.hits}
            if question.expects not in titles:
                misses.append(
                    f"{question.kind} {question.question!r} expected {question.expects!r}, "
                    f"got {sorted(titles)} (best {outcome.best_score:.3f})"
                )

    assert misses == [], "\n".join(misses)


async def test_a_hit_carries_everything_a_citation_needs(platform: Platform, cast: Cast) -> None:
    """**The ticket's citation line**: file name + page, the parent to quote, the scores.

    The four parts are asserted separately because each is a different way to get a
    citation wrong: a title instead of a file name, an inferred page instead of the
    parse's, the child's text quoted where the parent's context was meant, or a result
    with no score to decide anything with.
    """
    corpus = await index(platform, cast, *DOCUMENTS)
    async with service(platform) as retrieval:
        outcome = await retrieval.search("¿Cuántos días de permiso por matrimonio?")

    assert not outcome.insufficient_evidence, (
        f"the sample's own question came back below the threshold: {outcome.best_score:.3f}"
    )
    assert 1 <= len(outcome.hits) <= 5

    top = outcome.hits[0]
    assert top.document.id == UUID(corpus.ids["Politica de vacaciones y permisos"])
    assert top.document.title == "Politica de vacaciones y permisos"
    assert top.document.filename == "politica_vacaciones.md"
    assert top.page_from is None and top.page_to is None, (
        "a Markdown file has no pages; naming one would be an inferred citation"
    )
    assert top.content and top.quote
    assert top.context_scope in {"parent", "child"}
    if top.context_scope == "parent":
        assert top.parent_content is not None
        assert top.quote == top.parent_content
    assert top.vector_distance is not None or top.text_rank is not None
    assert top.fusion_score > 0.0
    assert 0.0 <= top.rerank_score <= 1.0
    assert top.text_rank_position == 1 or top.vector_rank == 1


async def test_a_pdf_hit_names_the_page_it_came_from(platform: Platform, cast: Cast) -> None:
    """**The other half of "file name + page"**: a real page number, from the parse.

    The sample corpus is Markdown, which has no pages — so this asks a PDF, where the
    page is a fact ticket 31 captured per page rather than something the retriever
    guesses. §5.2's citation is `《文件名》第 N 页`; without this the format is only half
    implemented.

    The second page holds a whole section, and thirteen sentences is more than a
    400-token child holds, so the split has to place the block there rather than
    spreading it across the page break.

    The PDF is the *only* document in this test: the Markdown sample also answers
    questions about marriage leave in fifteen days, and a corpus with both would make
    "the top hit is the PDF" a question about ranking rather than about the page a
    citation names.
    """
    from tests.support.documents import pdf_bytes

    filler = (
        "El personal con al menos un ano de antiguedad podra solicitar dias adicionales. "
        "La solicitud se presentara por escrito con quince dias de antelacion. "
    )
    content = pdf_bytes(
        "Indice de la politica. " + filler * 4,
        "El permiso por matrimonio es de quince dias naturales. " + filler * 13,
    )
    corpus = await index(platform, cast)
    response = await corpus.admin.post(
        "/api/v1/documents",
        files={"file": ("politica.pdf", content, "application/octet-stream")},
        data={
            "title": "Politica en PDF",
            "is_company_kb": "true",
            "department_id": cast.department,
        },
    )
    assert response.status_code == 201, response.text
    assert await run_parse(platform, response.json()["id"], embedder=DeterministicEmbedder())

    async with service(platform) as retrieval:
        outcome = await retrieval.search("permiso por matrimonio quince dias naturales")

    assert outcome.hits, "the PDF answered nothing"
    top = outcome.hits[0]
    assert top.document.filename == "politica.pdf"
    assert top.page_from is not None and top.page_to is not None
    # The citation names a page, and it is the page the answer is on. `page_from` may be 1
    # when the splitter's own sentence grouping carried the first sentence of the page-2
    # block into the chunk before it — that is a real chunk, and the range says so — so
    # what is asserted is that page 2 is inside the range and that the passage quoted is
    # the one that answers.
    assert top.page_to == 2, f"the citation stopped at page {top.page_to}"
    assert top.page_from <= 2
    assert "matrimonio" in top.quote.lower(), (
        f"the quoted context is not the passage that answers: {top.quote[:120]!r}"
    )


async def test_the_reranker_seam_is_used(platform: Platform, cast: Cast) -> None:
    """**The seam, reached.** A reranker that is built and never called is the failure
    mode of every extension point.

    The recording adapter proves the call happened and that it was handed the fused
    candidates — not the raw legs, which would mean the fusion was bypassed. And the
    default is asserted to be a `Reranker`, so a deployment that configured nothing
    still gets the stage §5.2 puts between the fusion and the top five.
    """
    await index(platform, cast, *DOCUMENTS[:1])
    recorder = RecordingReranker()
    async with service(platform, reranker=recorder) as retrieval:
        outcome = await retrieval.search("¿Cuántos días de vacaciones anuales retribuidas?")
        assert retrieval.reranker is recorder

    assert recorder.calls, "the reranker seam was never reached"
    query, count = recorder.calls[0]
    assert query == "¿Cuántos días de vacaciones anuales retribuidas?"
    assert count == len(outcome.fused), (
        "the reranker was handed something other than the fused candidates"
    )
    async with service(platform) as default:
        assert isinstance(default.reranker, LexicalReranker)


async def test_a_weak_query_returns_no_basis_rather_than_a_low_scoring_five(
    platform: Platform, cast: Cast
) -> None:
    """**Mutation test (b)**: return a weak top five instead of the explicit state and
    this fails.

    The ticket's 「当最高得分低于阈值时，判定为无依据并向上层明确返回该状态，不返回勉强的低分结果」.
    A nonsense question over a real corpus is the case: the legs return *something*
    (every chunk has a vector, so the vector leg always has twenty candidates), so a
    search that skipped the threshold would answer with five passages about nothing.
    """
    await index(platform, cast, *DOCUMENTS)
    async with service(platform) as retrieval:
        outcome = await retrieval.search("zxqv plugh frotz wumble", limit=5)

    assert outcome.insufficient_evidence, (
        "a question with no basis in the corpus was answered with results: "
        f"best {outcome.best_score:.3f} vs threshold {outcome.threshold:.3f}"
    )
    assert outcome.hits == ()
    assert outcome.best_score < outcome.threshold
    # The scores still travel, and the candidates are still readable: the refusal has to
    # be able to say how close it was, and the debug view has to show what was rejected.
    assert outcome.best_score > 0.0
    assert outcome.fused and outcome.reranked


async def test_the_threshold_boundary_is_pinned_from_both_sides(
    platform: Platform, cast: Cast
) -> None:
    """**One point above and one below**: the "no basis" rule has a boundary.

    The threshold is moved to the *measured* score rather than the test asserting a
    number the implementation chose: at exactly `best_score` the search answers (the
    comparison is `<`, and a boundary that excluded its own value would make "the best
    score is below the threshold" mean two things), and one epsilon above it, the same
    query returns nothing at all. A test at only one side would pass for a threshold
    that was ignored.
    """
    await index(platform, cast, *DOCUMENTS)
    question = "¿Cuántos días de vacaciones anuales retribuidas?"
    async with service(platform) as retrieval:
        baseline = await retrieval.search(question)
    assert baseline.hits and not baseline.insufficient_evidence
    measured = baseline.best_score

    async with service(platform, min_score=measured) as at_line_service:
        at_the_line = await at_line_service.search(question)
    async with service(platform, min_score=measured + 1e-6) as above_service:
        just_above = await above_service.search(question)

    assert not at_the_line.insufficient_evidence, (
        f"a score equal to the threshold was refused: {measured!r}"
    )
    assert at_the_line.hits and at_the_line.best_score == pytest.approx(measured)
    assert just_above.insufficient_evidence, (
        f"a score below the threshold was returned anyway: {measured!r}"
    )
    assert just_above.hits == ()
    assert just_above.threshold == pytest.approx(measured + 1e-6)


async def test_an_explicit_filter_hides_an_unreachable_document(
    platform: Platform, cast: Cast
) -> None:
    """**The filter entry point, applied**: a spec from the kernel decides what a search
    can reach, and the *unfiltered* search is visibly different.

    Ticket 35 pushes the real spec and owns the proof that nothing leaks; what this
    ticket owes is the entry point and the honesty about it. So the test does three
    things: an unfiltered search finds the document and says `filtered=False`; a search
    under the kernel's own spec for a caller in another department does not find it and
    says `filtered=True`; and a caller in the document's department does find it, so the
    filter is shown to *filter* rather than to empty every result.
    """
    await index(platform, cast, *DOCUMENTS[:1])
    query = "¿Cuántos días de permiso por matrimonio?"

    outsider = _principal(departments=frozenset({UUID(cast.other_department)}))
    insider = _principal(departments=frozenset({UUID(cast.department)}))
    async with service(platform) as retrieval:
        unfiltered = await retrieval.search(query)
        hidden = await retrieval.search(
            query, filter_spec=filter_for(outsider, ResourceKind.DOCUMENT)
        )
        visible = await retrieval.search(
            query, filter_spec=filter_for(insider, ResourceKind.DOCUMENT)
        )

    assert unfiltered.hits, "the sample document was not retrievable at all"
    assert unfiltered.filtered is False
    assert hidden.filtered is True
    assert all(
        hit.document.id != UUID(cast.department) for hit in hidden.hits
    ), "a filtered search returned the document and then said it was filtered"
    assert visible.filtered is True
    assert visible.hits, (
        "the spec for a caller in the document's own department reached nothing, so the "
        "filter is refusing rather than filtering"
    )


async def test_the_spec_the_search_applies_is_the_one_the_document_list_applies(
    platform: Platform, cast: Cast
) -> None:
    """One rule, two renderings, and they have to agree — which is the property that
    makes this module's own `WHERE` clause (§4.3's pre-filter) worth having.

    The same `FilterSpec` is handed to the document list and to the search, and the two
    must answer the same question about the same document. A retrieval clause that was
    *wider* than the list's would leak a passage; one that was narrower would hide a
    document from search that a user can open by hand.
    """
    from app.repositories.document import PostgresDocumentRepository

    corpus = await index(platform, cast, *DOCUMENTS[:2])
    insider_spec = filter_for(
        _principal(departments=frozenset({UUID(cast.department)})), ResourceKind.DOCUMENT
    )
    outsider_spec = filter_for(
        _principal(departments=frozenset({UUID(cast.other_department)})), ResourceKind.DOCUMENT
    )
    documents = platform.factory()
    try:
        listed = await PostgresDocumentRepository(documents).page_for(
            insider_spec, limit=50, offset=0
        )
        hidden_list = await PostgresDocumentRepository(documents).page_for(
            outsider_spec, limit=50, offset=0
        )
    finally:
        await documents.close()
    listed_ids = {document.id for document in listed.items}
    assert set(corpus.ids.values()) <= {str(value) for value in listed_ids}

    async with service(platform) as retrieval:
        hidden_search = await retrieval.search(
            "vacaciones anuales retribuidas", filter_spec=outsider_spec
        )

    assert {document.id for document in hidden_list.items}.isdisjoint(
        {UUID(value) for value in corpus.ids.values()}
    )
    assert {
        UUID(value) for value in corpus.ids.values()
    }.isdisjoint({hit.document.id for hit in hidden_search.hits})


async def test_a_deployment_without_vectors_answers_from_full_text_and_says_so(
    platform: Platform, cast: Cast
) -> None:
    """`EMBEDDING_PROVIDER=none` is a supported deployment, not a broken one.

    §5.3's degradation rule is "only errors and timeouts trigger degradation", and a
    missing provider is the configuration rather than an incident. The search answers
    from the text leg alone — the `search_vector` column and its GIN index are written
    by the pipeline regardless of the embedding provider, which
    `tests/test_embeddings.py` already asserts — and `legs_used` says which half ran, so
    a caller cannot mistake a text-only answer for a hybrid one.
    """
    await index(platform, cast, *DOCUMENTS[:1])
    # A question made only of stopwords matches no lexeme at all, and with no vector leg
    # either it would retrieve nothing for a reason that has nothing to do with the
    # degradation under test — so the query keeps its content words.
    async with service(platform, embedding=False) as retrieval:
        outcome = await retrieval.search("permiso matrimonio quince días naturales")

    assert outcome.legs_used == (RetrievalLeg.TEXT,)
    assert outcome.embedder is None
    assert not outcome.vector_candidates
    assert outcome.text_candidates
    assert outcome.hits, "the text leg alone found nothing in a corpus it indexed"


# --- part 4: the debug view --------------------------------------------------


async def test_the_debug_view_shows_both_legs_the_fusion_and_why_others_were_dropped(
    platform: Platform, cast: Cast
) -> None:
    """**The ticket's debug view**, as a value rather than as a screenshot.

    What it has to answer is 「为什么没检索到」, which needs four things: each leg's
    ranking, the fusion scores, the reranker's contribution, and — for everything that
    did not make the five — which stage dropped it. The test asserts each of those and
    then asserts the one property that makes the view trustworthy: it is the *same* run
    the ordinary search makes, so its "kept" set is exactly what a user would see.
    """
    await index(platform, cast, *DOCUMENTS)
    query = "¿Cuántos días de vacaciones anuales retribuidas?"

    async with service(platform) as retrieval:
        trace = await retrieval.explain(query, limit=5)
        plain = await retrieval.search(query, limit=5)

    assert trace.leg_limit == 20
    assert len(trace.outcome.vector_candidates) <= 20
    assert len(trace.outcome.text_candidates) <= 20
    assert trace.outcome.fused
    assert [hit.chunk_id for hit in trace.outcome.hits] == [hit.chunk_id for hit in plain.hits]
    assert [row.candidate.chunk_id for row in trace.kept] == [
        hit.chunk_id for hit in plain.hits
    ]

    kept = trace.kept
    assert len(kept) == len(plain.hits) and kept, "the debug view kept nothing"
    for row in kept:
        assert row.outcome == "kept"
        assert row.fusion_score > 0.0
        assert row.vector_rank is not None or row.text_rank_position is not None
        assert row.rerank_score is not None and row.rerank_rank is not None
        assert plain.hits[[hit.chunk_id for hit in plain.hits].index(row.candidate.chunk_id)]

    # Everything the fusion produced is accounted for: either kept, or dropped with a
    # reason that names the stage. A view that showed only the kept five could not
    # answer the question it exists for.
    assert len(trace.candidates) == len(trace.outcome.reranked)
    for row in trace.dropped:
        assert row.outcome in {"below_threshold", "outranked", "below_top_n"}
        assert row.reason, f"{row.candidate.chunk_id} was dropped with no reason"
    assert {row.outcome for row in trace.dropped} <= {
        "below_threshold",
        "outranked",
        "below_top_n",
    }
    # A dropped candidate is one the fusion saw — which is what makes "the fusion never
    # put it in the reranked window" a distinguishable answer from "the reranker demoted
    # it", and both are readable from the trace.
    for row in trace.dropped:
        assert row.rerank_rank is not None

    # The reranker's own contribution is carried per candidate, so a reader can see
    # whether the order came from the fusion or from the lexical stage.
    assert all(0.0 <= row.breakdown.score <= 1.0 for row in trace.outcome.reranked)
    assert trace.outcome.reranked[0].breakdown.prior > 0.0


async def test_a_service_driven_without_a_filter_says_so_in_the_debug_view(
    platform: Platform, cast: Cast
) -> None:
    """The module's own record of an unfiltered run, which no request path can produce.

    A `RetrievalService` can still be driven with no spec — the offline evaluation does,
    deliberately, and says so through `filtering.unfiltered()` — and the trace reports
    that as `filtered: false` with no predicate. The distinction the ticket turns on is
    that this is a *decision* at the call site rather than a default a route could fall
    into: the two HTTP routes below push a spec on every request, and the escalation
    suite asserts what that spec keeps out of the hit set.
    """
    await index(platform, cast, *DOCUMENTS[:1])
    async with service(platform) as retrieval:
        trace = await retrieval.explain("vacaciones", filter_spec=unfiltered())

    assert trace.outcome.filtered is False
    assert trace.filter_explanation is None, "an unfiltered run printed a predicate"
    assert all(row.reason for row in trace.candidates)
    assert trace.leg_limit == 20


# --- part 5: the two HTTP surfaces -------------------------------------------


async def test_the_search_endpoint_answers_with_the_fused_five(
    platform: Platform, cast: Cast
) -> None:
    """Through the router: the guard, the response shape and the `filtered` flag.

    The route's own composition — settings-driven embedder, fusion constant and
    threshold — is only reachable here, and so is the fact that a permitted caller gets
    a body a client can cite from.

    **`filtered` is `true`, and since ticket 35 it never says anything else here.** The
    route resolves the caller's spec through `answer_filter_for` and pushes it into both
    legs, so `false` on this response would be a request that searched the whole corpus —
    which is the fact the field exists to make impossible to overlook.
    """
    corpus = await index(platform, cast, *DOCUMENTS)
    response = await corpus.admin.get(
        "/api/v1/retrieval/search",
        params={"q": "permiso por matrimonio quince días naturales"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["insufficient_evidence"] is False
    assert body["filtered"] is True, "a request searched without a permission condition"
    assert body["legs_used"] == ["vector", "text"]
    assert body["fusion_k"] == 60
    assert 1 <= len(body["hits"]) <= 5
    top = body["hits"][0]
    assert top["document"]["filename"]
    assert top["quote"]
    assert top["fusion_score"] > 0 and top["rerank_score"] > 0
    assert body["vector_candidates"] > 0 and body["text_candidates"] > 0


async def test_the_search_endpoint_answers_an_empty_question_with_a_catalogued_refusal(
    platform: Platform, cast: Cast
) -> None:
    """An empty query is a 422 with the module's own code, not a confident empty answer.

    "You asked nothing" and "the knowledge base holds no basis for this" are different
    answers and a client shows different copy for each; conflating them is how a user
    learns that the assistant is useless rather than that they sent a blank box.
    """
    from app.core.errors import ErrorCode

    response = await cast.uploader.get("/api/v1/retrieval/search", params={"q": "   "})

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == ErrorCode.RETRIEVAL_QUERY_INVALID.value
    # A blank query is refused at the *service*, not by the query-parameter validator:
    # `q="   "` satisfies `min_length=1`, and the honest place for "this is not a
    # question" is the module that would otherwise embed it and search with it.
    from app.domain.errors import DomainError
    from app.domain.retrieval.errors import require_query

    with pytest.raises(DomainError) as refusal:
        require_query("")
    assert refusal.value.code is ErrorCode.RETRIEVAL_QUERY_INVALID


async def test_the_debug_endpoint_is_administration_and_hr_only(
    platform: Platform, cast: Cast
) -> None:
    """**The authorised view, from both sides**: the two roles that own the knowledge
    base reach it, and an ordinary caller is refused with a catalogued code.

    The refusal is asserted for *content* as well as status: a debug body that leaked the
    fusion's arithmetic to an unauthorised caller would be the disclosure the guard
    exists to prevent, and a `detail` that named the corpus would leak through the
    error envelope.

    The document is uploaded by an administrator and *read back* by one, because that is
    the only caller with an unfiltered view of it — see
    `test_the_debug_endpoint_shows_the_filter_a_run_applied`.
    """
    from app.core.errors import ErrorCode

    corpus = await index(platform, cast, *DOCUMENTS[:1])
    params = {"q": "permiso matrimonio quince días naturales"}

    refused = await cast.uploader.get("/api/v1/retrieval/debug", params=params)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == ErrorCode.FORBIDDEN.value
    assert "fusion_score" not in refused.text

    allowed = await corpus.admin.get("/api/v1/retrieval/debug", params=params)
    assert allowed.status_code == 200, allowed.text
    body = allowed.json()
    assert body["leg_limit"] == 20
    assert body["vector_leg"] and body["text_leg"]
    assert body["candidates"] and body["kept"]
    assert body["outcome"]["hits"], "the debug view found nothing for a sample question"
    assert body["kept"][0]["document_id"] == corpus.ids["Politica de vacaciones y permisos"]
    for row in body["vector_leg"]:
        assert row["vector_rank"] is not None
        assert row["reason"]
    for row in body["text_leg"]:
        assert row["text_rank_position"] is not None


async def test_the_debug_endpoint_shows_the_filter_a_run_applied(
    platform: Platform, cast: Cast
) -> None:
    """**Ticket 35's「调试视图中显示本次生效的权限条件」**, on this ticket's surface.

    The request path now pushes a spec on every call, so the view carries the predicate
    the database was given, verbatim, and the two halves that matter are asserted: the
    explanation is §4.2's *disjunction* — the rule is four alternatives, and an
    explanation that showed one conjunct would suggest a search that can never answer —
    and it names *this caller's* values, so a reviewer reads the departments they reach,
    the clearance levels inside their ceiling, and their own employee id for the
    ownership clause.

    The second claim is the one that makes the view worth reading: the predicate the
    debug view reports is the same predicate the *service* built for that caller, not a
    re-description printed by a second translation. The escalation suite pins the last
    step — that the document the predicate excludes is absent from the hit set.
    """
    corpus = await index(platform, cast, *DOCUMENTS[:1])
    params = {"q": "¿Cuántos días de permiso por matrimonio?"}
    admin = corpus.admin

    body = (await admin.get("/api/v1/retrieval/debug", params=params)).json()
    assert body["filtered"] is True, "the debug view ran on a request path unfiltered"
    assert body["filter_explanation"], "the view printed no permission condition"

    # The route's own principal, resolved from the real snapshot rather than hand-built:
    # a `Principal` this test made up could carry a clearance the snapshot never gives
    # anybody, and then "the view's predicate is the helper's" would be an equality
    # between two things that are not the same decision.
    from app.domain.access.snapshot import resolve_principal

    async with platform.factory() as session:
        principal = await resolve_principal(session, UUID(admin.user_id))
    assert principal is not None
    expected = retrieval_filter_explanation(
        filter_for(principal, ResourceKind.DOCUMENT)
    )

    assert body["filter_explanation"] == expected, (
        "the predicate the view reported is not the one this caller's spec renders:\n"
        f"view:     {body['filter_explanation']}\n"
        f"expected: {expected}"
    )
    assert "d.owner_employee_id" in body["filter_explanation"]
    assert " OR " in body["filter_explanation"], (
        "the explanation is not §4.2's disjunction: " + body["filter_explanation"]
    )
    assert str(cast.department) in body["filter_explanation"], (
        "the explanation does not name the department the caller reaches"
    )
    assert str(cast.other_department) not in body["filter_explanation"], (
        "the explanation names a department the caller does not reach"
    )
    assert str(admin.employee_id) in body["filter_explanation"], (
        "the explanation omits §4.2's ownership clause"
    )


async def test_the_debug_endpoint_shows_what_the_rerank_window_dropped(
    platform: Platform, cast: Cast
) -> None:
    """The other half of 「为什么没检索到」: candidates the fusion saw and the answer did not.

    A `limit` of one guarantees the view has something to explain away, and the reason
    has to name the stage — the fusion's window or the reranker's score — because those
    are two different investigations. Without this the view would answer "it is not in
    the results", which is what the user already knew.
    """
    corpus = await index(platform, cast, *DOCUMENTS)
    admin = corpus.admin
    response = await admin.get(
        "/api/v1/retrieval/debug", params={"q": "vacaciones anuales días", "limit": 1}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["kept"]) == 1
    assert body["dropped"], "a one-result search dropped nothing, so the fixture is wrong"
    assert all(row["reason"] for row in body["dropped"])
    assert all(
        row["outcome"] in {"outranked", "below_top_n", "below_threshold"}
        for row in body["dropped"]
    )
    # Every kept row is accounted for in the full candidate list, so the view's three
    # lists cannot disagree with each other.
    kept_ids = {row["chunk_id"] for row in body["kept"]}
    assert kept_ids <= {row["chunk_id"] for row in body["candidates"]}
    assert {row["chunk_id"] for row in body["candidates"]} == (
        kept_ids | {row["chunk_id"] for row in body["dropped"]}
    )


__all__ = ["Corpus", "index", "service", "upload_document"]
