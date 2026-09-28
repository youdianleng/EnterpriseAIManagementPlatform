"""Offline retrieval evaluation: three modes over one question set, and what each number is
a number *about*.

    # against a scratch database, as the tests do
    docker compose exec -T -e TEST_DATABASE_NAME=eam_eval api \
        python tests/tools/eval_retrieval.py --sample

    # or with your own questions, in the format below
    docker compose exec -T -e TEST_DATABASE_NAME=eam_eval api \
        python tests/tools/eval_retrieval.py questions.jsonl

Input is JSON Lines, one question per line — `questions.example.jsonl` beside this script
is a three-line one to copy:

    {"question": "¿Cuántos días de vacaciones?", "documents": ["Politica de vacaciones"]}
    {"question": "How many days of leave?", "documents": ["Politica", "Convenio"]}

`documents` are document **titles** (the `documents.title` column), not file names — a title
is what a citation shows and what a person writes when they build the fixture. A question may
name several documents, and any one of them counting as a hit is the usual reading.

## What it measures, and why three modes

Ticket 33's acceptance line is 「混合检索的命中率不低于纯向量检索」 — hybrid must not be worse
than vector-only — and that line is only checkable against the same questions at the same
`k`. So one run reports all three, on the same corpus and the same question set:

* **`vector`** — the vector leg's top `k` alone, which is the exact baseline ticket 32's
  script measured and the literal left-hand side of the acceptance line: "the *results*
  pure vector search would have returned" are its first `k`, not its first `leg_limit`.
* **`text`** — the full-text leg's top `k` alone, ranked by `ts_rank_cd`. The half §5.2 says
  pure vector search is missing: 制度编号、缩写、专有名词.
* **`hybrid`** — both legs at `leg_limit`, fused by the *real* RRF, reranked by the *real*
  reranker, thresholded by the *real* threshold, top `k`. This is the pipeline a question
  actually runs (`RetrievalService.search`), not a re-implementation of it — the whole point
  of measuring it is that the number is about the code that serves requests.

Per mode:

* **hit@k** — the fraction of questions whose top-k results include a chunk of one of the
  expected documents.
* **MRR** — the mean reciprocal rank of the first hit, printed beside it because hit@k hides
  whether the right document came first or fifth, and "fifth" is not good enough to put in
  front of a model with a small context window.
* **coverage** — how many questions had at least one expected document that is *indexed*: a
  document that is missing, not `ready`, or has no vectors is excluded from the denominator
  and reported separately. Without this the script would report a low hit rate for a corpus
  that simply has not been parsed yet, which is the one wrong answer that wastes an afternoon.

## What it does not measure

* **Not semantic quality, when the provider is the fake.** With `EMBEDDING_PROVIDER=fake` —
  the default outside production — the vectors are hashed bag-of-words, so every number is a
  *lexical* score: a question that shares no words with the right chunk will miss however
  good the retrieval is, and paraphrases are under-counted. The script prints the provider
  and the model it used on every run, and a number from the fake must not be compared with a
  number from `text-embedding-3-small`.
* **Not permissions.** It runs with `app.current_system` published, so it sees every document
  — including ones the question's asker could not read. That is right for measuring retrieval
  and wrong for measuring *leakage*; the leak test is ticket 30's, over the runtime role with
  a real caller context, and ticket 35's over the filter.
* **Not the pipeline's freshness.** It reads what is stored. A document that failed to parse,
  failed to embed, or was archived is counted as missing coverage rather than retried.
* **Not generation.** Nothing here calls a model. `insufficient_evidence` results count as
  misses, which is the honest reading of the threshold: the design's answer to a question
  below it is a refusal, not a citation.
* **Not a benchmark.** `--sample` is nine questions over three documents. Nine questions can
  distinguish "the pipeline works" from "it does not"; they cannot establish that RRF beats a
  weighted sum on a real corpus. A real evaluation needs the organisation's own documents and
  its own questions, which is what the JSON-Lines input is for.

Exit code is 0 whatever the hit rates: this is a measurement, not a check. A deployment that
runs it in CI wants `--min-hit-rate` (and gets a non-zero exit below it), which is the
threshold somebody has to choose deliberately rather than inherit.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

# `python tests/tools/eval_retrieval.py` puts `tests/tools` on `sys.path`, not the
# repository root, so `import app` would fail — and the command in this module's docstring
# is the one an operator will run. Adding the root here is what makes that command work
# without a `PYTHONPATH` prefix and without `-m`, which the tests' own modules do not need
# because pytest adds the root for them.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402 - after the path fix above

#: How many results each ranking returns per question. Five is the usual figure for a
#: context window that also has to hold a conversation, and it is the top-k the three
#: modes are compared at.
DEFAULT_TOP_K = 5

#: The system flag the pipeline publishes. A measurement is not a request and has no caller,
#: so it declares itself the system — see `document.service.publish_system_context`. The
#: consequence is stated in the module docstring: this sees every document.
SYSTEM_SETTING = "app.current_system"

MODES = ("vector", "text", "hybrid")


def point_at_the_scratch_database() -> None:
    """Retarget the process at `TEST_DATABASE_NAME`, before anything imports `app.config`.

    `tests/conftest.py` does the same thing for the suite, and for the same reason: a
    measurement must not run against the development database. It matters twice over here
    because `--sample` *writes* — it seeds three documents — and an evaluation script that
    seeded the development corpus would make the deployment's answers depend on whether
    somebody had run an experiment.
    """
    name = os.environ.get("TEST_DATABASE_NAME")
    if not name:
        return
    for variable, fallback in (
        ("DATABASE_URL", "postgresql+psycopg://eam:eam_dev_password@postgres:5432/eam"),
        ("APP_DATABASE_URL", None),
    ):
        value = os.environ.get(variable) or fallback
        if not value:
            continue
        prefix, _, _ = value.rpartition("/")
        os.environ[variable] = f"{prefix}/{name}"


@dataclass(frozen=True, slots=True)
class Question:
    question: str
    documents: tuple[str, ...]
    line: int


@dataclass(frozen=True, slots=True)
class Outcome:
    question: Question
    ranks: dict[str, list[str]]
    indexed: tuple[str, ...]
    missing: tuple[str, ...]
    #: The vector leg's deeper list, which is what the fusion saw. Kept for the report:
    #: "the baseline missed it because it sat at rank 7" is a different finding from
    #: "neither leg found it", and only this makes the two distinguishable.
    deep_vector: list[str] = field(default_factory=list)

    def hit_rank(self, mode: str) -> int | None:
        return next(
            (
                position
                for position, title in enumerate(self.ranks[mode], start=1)
                if title in self.indexed
            ),
            None,
        )

    def deep_vector_rank(self) -> int | None:
        return next(
            (
                position
                for position, title in enumerate(self.deep_vector, start=1)
                if title in self.indexed
            ),
            None,
        )


@dataclass
class Score:
    """One mode's numbers, accumulated over the questions that could be scored."""

    mode: str
    hits: list[int] = field(default_factory=list)
    scored: int = 0

    @property
    def hit_rate(self) -> float:
        return len(self.hits) / self.scored if self.scored else 0.0

    @property
    def mrr(self) -> float:
        return statistics.fmean(1.0 / rank for rank in self.hits) if self.hits else 0.0


def load(path: Path) -> list[Question]:
    """The questions, with the line number kept for an error message that names one."""
    questions: list[Question] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        body = line.strip()
        if not body or body.startswith("//") or body.startswith("#"):
            continue
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as error:
            raise SystemExit(f"{path}:{number}: not JSON: {error}") from error
        question = str(parsed.get("question", "")).strip()
        documents = tuple(
            str(item).strip() for item in parsed.get("documents", []) if str(item).strip()
        )
        if not question or not documents:
            raise SystemExit(
                f"{path}:{number}: a question needs both a `question` and at least one "
                f"`documents` title"
            )
        questions.append(Question(question=question, documents=documents, line=number))
    if not questions:
        raise SystemExit(f"{path}: no questions in it")
    return questions


def sample_questions() -> list[Question]:
    """The shipped sample, as the same value a JSON-Lines file produces."""
    from tests.support.retrieval_sample import QUESTIONS

    return [
        Question(question=item.question, documents=(item.expects,), line=number)
        for number, item in enumerate(QUESTIONS, start=1)
    ]


async def seed_sample(session, *, reset: bool) -> int:
    """Put the shipped sample corpus into the scratch database, and answer how many landed.

    **This is the one place the script writes**, and it writes because a measurement of
    retrieval needs something to retrieve: a scratch database is empty, and a hit rate over
    no documents is a number about arithmetic. It goes through the *real* pipeline —
    `DocumentService.parse_document`, the same call the job makes — so what is measured is
    the chunks, the `search_vector` and the vectors a real upload produces, not rows a
    script invented. `principal=None` is the honest shape for that, exactly as the job uses
    it, and the connection is the owner's.

    `reset` deletes the sample first, so a second run measures the same corpus rather than
    a corpus with two copies of it in.
    """
    from app.config import get_settings
    from app.domain.document.embeddings import DeterministicEmbedder
    from app.domain.document.models import DocumentMetadata
    from app.domain.document.service import DocumentService
    from app.domain.document.storage import LocalFileStore
    from app.jobs.parse_documents import system_session
    from app.repositories.document import PostgresDocumentRepository
    from tests.support.retrieval_sample import DOCUMENTS

    titles = [document.title for document in DOCUMENTS]
    if reset:
        await session.execute(
            text("DELETE FROM documents WHERE title = ANY(CAST(:titles AS text[]))"),
            {"titles": titles},
        )
        await session.commit()

    present = set(
        (
            await session.execute(
                text("SELECT title FROM documents WHERE title = ANY(CAST(:titles AS text[]))"),
                {"titles": titles},
            )
        ).scalars()
    )
    waited = [document for document in DOCUMENTS if document.title not in present]
    if not waited:
        return 0

    # `uploaded_by_employee_id` and the department are foreign keys, and a company
    # knowledge-base document is refused by a CHECK unless it has a department. An existing
    # employee is found rather than created: this is a measurement, and a seeded person
    # row would be a fact nothing in the corpus accounts for.
    department = await session.scalar(
        text("SELECT id FROM departments WHERE is_active ORDER BY path LIMIT 1")
    )
    uploader = await session.scalar(text("SELECT id FROM employees ORDER BY hire_date LIMIT 1"))
    if department is None:
        department = uuid4()
        await session.execute(
            text(
                """
                INSERT INTO departments (id, code, name_es, name_en, path, depth,
                                         clearance_level, is_active)
                VALUES (:id, 'eval-kb', 'Base de conocimiento (evaluación)',
                        'Knowledge base (evaluation)', 'eval_kb', 0, 'low', true)
                """
            ),
            {"id": department},
        )
        await session.commit()

    if uploader is None and not reset:
        # A document references its uploader, and a scratch database the suite has never
        # touched has nobody. Reported rather than seeded: a person row an evaluation
        # script invented would be a fact nothing else in the installation accounts for.
        raise SystemExit(
            "no employee row in "
            f"{await session.scalar(text('SELECT current_database()'))}: create one with "
            "`docker compose exec -T postgres psql -U eam -d <db> -c \"INSERT INTO employees "
            "(id, first_name, last_name, email, hire_date, status) VALUES "
            "(gen_random_uuid(), 'Ana', 'Martin', 'eval@empresa.es', '2024-01-15', "
            "'active')\"` and run this again."
        )

    storage = LocalFileStore(get_settings().document_storage_path)
    for document in waited:
        repository = PostgresDocumentRepository(session)
        body = document.body.encode("utf-8")
        key = storage.put(body, extension=".md")
        stored = await repository.create(
            title=document.title,
            # A company knowledge base: §4.2 reaches one of those through its department,
            # and the measurement runs as the system, so either kind would do — the company
            # shape is the one a real policy corpus has.
            owner_employee_id=None,
            uploaded_by_employee_id=uploader,
            metadata=DocumentMetadata(
                title=document.title, is_company_kb=True, department_id=department
            ),
            storage_path=key,
            content_sha256="0" * 64,
            filename=document.filename,
            media_type="text/markdown",
            file_size=len(body),
        )
        await repository.commit()

        # The pipeline, driven exactly as the job drives it: one session, one commit, the
        # system flag published so the row-level policies admit it.
        async with system_session() as job_session:
            service = DocumentService(
                PostgresDocumentRepository(job_session),
                job_session,
                principal=None,  # type: ignore[arg-type]
                storage=storage,
                embedder=DeterministicEmbedder(),
            )
            await service.parse_document(stored.id)
            await job_session.commit()
    return len(waited)


async def coverage(session, questions: list[Question]) -> dict[str, tuple[str, int]]:
    """Expected title → (document id, embedded child chunks), for the ones that are indexable.

    A title that is not in `documents`, or is not `ready`, or has no embedded child, is not
    in this mapping — and the caller reports those separately rather than counting them as
    misses. For the hybrid mode a document with no vector can still be found by the text leg,
    which is why the count is reported rather than used as a filter: what makes a question
    scorable is that its document is *there*.
    """
    titles = sorted({title for question in questions for title in question.documents})
    found: dict[str, tuple[str, int]] = {}
    for title in titles:
        row = (
            await session.execute(
                text(
                    """
                    SELECT d.id::text,
                           (SELECT count(*) FROM document_chunks c
                             WHERE c.document_id = d.id AND c.parent_chunk_id IS NOT NULL)
                      FROM documents d
                     WHERE d.title = :title AND d.status = 'ready'
                     ORDER BY d.created_at
                     LIMIT 1
                    """
                ),
                {"title": title},
            )
        ).first()
        if row is not None and int(row[1]) > 0:
            found[title] = (row[0], int(row[1]))
    return found


async def vector_misses(session, questions: list[Question]) -> dict[str, int]:
    """How many expected documents have no vector at all — the fake-provider blind spot."""
    counts: dict[str, int] = {}
    for title in sorted({title for question in questions for title in question.documents}):
        counts[title] = int(
            await session.scalar(
                text(
                    """
                    SELECT count(*) FROM document_chunks c
                      JOIN documents d ON d.id = c.document_id
                     WHERE d.title = :title AND c.parent_chunk_id IS NOT NULL
                       AND c.embedding IS NULL
                    """
                ),
                {"title": title},
            )
            or 0
        )
    return counts


class LegRanking:
    """The two legs for one question, from the real repository.

    The legs are read through `PostgresChunkSearchRepository`, so `vector` and `text` here
    are the *same* SQL and the *same* ranks the hybrid mode fuses — a measurement whose
    halves were re-implemented for the occasion would be a measurement of the
    re-implementation. `embedding=None` is the `EMBEDDING_PROVIDER=none` deployment, where
    the vector leg does not run and the text leg answers alone.
    """

    def __init__(self, session, *, leg_limit: int = 20) -> None:  # noqa: ANN001 - AsyncSession
        from app.repositories.retrieval import PostgresChunkSearchRepository

        self._repository = PostgresChunkSearchRepository(session)
        self._leg_limit = leg_limit

    async def rankings(
        self, embedder, question: str, top_k: int
    ) -> tuple[list[str], list[str], list[str]]:
        """`(vector at top_k, vector at the leg limit, text at top_k)`.

        The two vector lists answer two different questions and the evaluation needs both.
        "What would pure vector search have returned?" is its first `top_k` — that is the
        baseline the acceptance line is about — while the fusion sees the deeper list, and
        reporting only the deep one would credit the baseline with recall it never had.
        """
        probe: list[float] | None = None
        if embedder is not None:
            vectors = await embedder.embed([question])
            probe = vectors[0] if vectors else None
        vector_at_k, _ = await self._repository.search_legs(
            question, embedding=probe, leg_limit=top_k
        )
        if self._leg_limit == top_k:
            deep = vector_at_k
        else:
            deep, _ = await self._repository.search_legs(
                question, embedding=probe, leg_limit=self._leg_limit
            )
        _, textual = await self._repository.search_legs(
            question, embedding=probe, leg_limit=top_k
        )
        return (
            [row.document_title for row in vector_at_k],
            [row.document_title for row in deep],
            [row.document_title for row in textual],
        )


async def evaluate(
    questions: list[Question],
    top_k: int,
    *,
    reranker_name: str | None,
    min_score: float,
    fusion_k: int,
    leg_limit: int,
) -> tuple[list[Outcome], dict[str, str]]:
    """Run every question and answer with the outcomes and what produced the numbers."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.config import get_settings
    from app.db import build_engine
    from app.domain.document.embeddings import build_embedder
    from app.domain.retrieval.rerank import build_reranker
    from app.domain.retrieval.service import RetrievalService
    from app.repositories.retrieval import PostgresChunkSearchRepository

    settings = get_settings()
    embedder = build_embedder(
        settings.embeddings_provider,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )

    engine = build_engine(settings, settings.database_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    outcomes: list[Outcome] = []
    try:
        async with factory() as session:
            await session.execute(
                text("SELECT set_config(:name, 'true', true)"), {"name": SYSTEM_SETTING}
            )
            available = await coverage(session, questions)
            service = RetrievalService(
                PostgresChunkSearchRepository(session),
                embedder=embedder,
                fusion_k=fusion_k,
                min_score=min_score,
                leg_limit=leg_limit,
                reranker=build_reranker(reranker_name),
            )
            for question in questions:
                vector_at_k, vector_deep, text_at_k = await LegRanking(
                    session, leg_limit=leg_limit
                ).rankings(embedder, question.question, top_k)
                hybrid = await service.search(question.question, limit=top_k)
                indexed = tuple(title for title in question.documents if title in available)
                missing = tuple(title for title in question.documents if title not in available)
                outcomes.append(
                    Outcome(
                        question=question,
                        ranks={
                            "vector": vector_at_k,
                            "text": text_at_k,
                            "hybrid": [hit.document.title for hit in hybrid.hits],
                        },
                        indexed=indexed,
                        missing=missing,
                        # Not a mode — the deeper list the fusion saw, kept so a miss can
                        # be explained: a question the baseline missed because the answer
                        # was at rank 7 is a different finding from one neither leg found.
                        deep_vector=vector_deep,
                    )
                )
    finally:
        await engine.dispose()

    from app.core.constants import EMBEDDING_MODEL

    return outcomes, {
        "provider": settings.embeddings_provider,
        "model": (embedder.name if embedder is not None else "none"),
        "constants_model": EMBEDDING_MODEL,
        "reranker": build_reranker(reranker_name).name,
        "fusion_k": str(fusion_k),
        "threshold": f"{min_score:.2f}",
    }


def report(
    outcomes: list[Outcome],
    top_k: int,
    facts: dict[str, str],
) -> dict[str, Score]:
    """Print the three modes side by side, and answer with their scores."""
    scorable = [outcome for outcome in outcomes if outcome.indexed]
    scores: dict[str, Score] = {}
    for mode in MODES:
        score = Score(mode=mode, scored=len(scorable))
        score.hits = [
            rank for outcome in scorable if (rank := outcome.hit_rank(mode)) is not None
        ]
        scores[mode] = score

    print(
        f"provider: {facts['provider']}   model: {facts['model']}   "
        f"reranker: {facts['reranker']}"
    )
    if facts["provider"] == "fake":
        print(
            "NOTE: the fake provider's vectors are hashed bag-of-words, so the `vector` and\n"
            "      `hybrid` numbers are LEXICAL. Do not compare them with a run against a\n"
            "      real embedding model — and do not read `hybrid >= vector` here as a claim\n"
            "      about semantic retrieval."
        )
    print(
        f"questions: {len(outcomes)}   scored: {len(scorable)}   top-k: {top_k}   "
        f"fusion k: {facts['fusion_k']}   threshold: {facts['threshold']}"
    )
    print()
    print(f"{'mode':<8}{'hit@' + str(top_k):>10}{'MRR':>10}")
    for mode in MODES:
        score = scores[mode]
        print(f"{mode:<8}{score.hit_rate:>10.3f}{score.mrr:>10.3f}")
    print()

    if scores["hybrid"].hit_rate < scores["vector"].hit_rate:
        print(
            "** hybrid is WORSE than vector-only on this question set. That is the ticket's\n"
            "   acceptance line failing, and it is reported rather than tuned away: the\n"
            "   sample may be one where fusion dilutes the one leg that works, the reranker\n"
            "   may be promoting a wrong passage, or `fusion k` may need measuring.\n"
        )
    # The interesting failure the acceptance line is about: the answer was inside the
    # fusion's window and outside the five the baseline returned. A miss *within* the
    # window is exactly what a second leg and a fusion can recover, and counting them makes
    # the comparison say something even on a corpus where both modes score 1.000.
    recovered = [
        outcome
        for outcome in scorable
        if outcome.hit_rank("vector") is None and outcome.hit_rank("hybrid") is not None
    ]
    deeper = [
        outcome
        for outcome in scorable
        if outcome.hit_rank("vector") is None
        and (rank := outcome.deep_vector_rank()) is not None
        and rank <= 20
    ]
    print(
        f"questions where the baseline (vector top {top_k}) missed and the hybrid found: "
        f"{len(recovered)}"
    )
    print(
        f"  ... of which the answer was inside the vector leg's own top 20: {len(deeper)}"
        "  (a deeper window is what recovers these, whether by one leg or two)"
    )
    print()

    for outcome in outcomes:
        if not outcome.indexed:
            expected = list(outcome.question.documents)
            print(f"  line {outcome.question.line}: NOT SCORED - none of {expected} is indexed")
            continue
        marks = " ".join(
            f"{mode[:1]}="
            + (f"hit@{outcome.hit_rank(mode)}" if outcome.hit_rank(mode) else "MISS")
            for mode in MODES
        )
        print(f"  {marks:<34} {outcome.question.question[:60]}")
        if outcome.hit_rank("hybrid") is None:
            print(f"        expected: {list(outcome.question.documents)}")
            for mode in MODES:
                print(f"        {mode + ':':<8} {outcome.ranks[mode]}")
        if outcome.missing:
            print(f"        also expected, not indexed: {list(outcome.missing)}")
    return scores


async def _seed(settings, *, reset: bool) -> None:  # noqa: ANN001 - Settings
    """Seed the sample into the scratch database, in its own session and engine."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db import build_engine

    engine = build_engine(settings, settings.database_url)
    try:
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            seeded = await seed_sample(session, reset=reset)
        print(f"seeded {seeded} sample document(s) into {settings.database_url.rpartition('/')[2]}")
    finally:
        await engine.dispose()


async def missing_vectors(questions: list[Question]) -> dict[str, int]:
    """Expected documents with children that have no vector — reported after the numbers."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.config import get_settings
    from app.db import build_engine

    settings = get_settings()
    engine = build_engine(settings, settings.database_url)
    try:
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            return await vector_misses(session, questions)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "questions",
        nargs="?",
        type=Path,
        help="JSON Lines: question + expected titles. Omit with --sample.",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="measure the sample shipped in tests/support/retrieval_sample.py",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "with --sample: delete and re-seed the sample corpus first, so a second run "
            "measures the same corpus rather than two copies of it"
        ),
    )
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument(
        "--leg-limit",
        type=int,
        default=None,
        help=(
            "how deep each leg goes before the fusion; defaults to the configured setting "
            "(20). Setting it equal to --top-k measures fusion against a single leg at the "
            "*same* recall budget"
        ),
    )
    parser.add_argument(
        "--fusion-k",
        type=int,
        default=None,
        help="the RRF constant; defaults to the configured setting",
    )
    parser.add_argument(
        "--min-hit-rate",
        type=float,
        default=None,
        help=(
            "exit non-zero below this on the *hybrid* mode; unset means the run is a "
            "measurement, not a check"
        ),
    )
    arguments = parser.parse_args(argv)

    point_at_the_scratch_database()

    from app.config import get_settings

    settings = get_settings()
    if arguments.sample and arguments.questions is not None:
        raise SystemExit("give either a questions file or --sample, not both")
    if arguments.questions is None and not arguments.sample:
        raise SystemExit("give a questions file, or --sample for the shipped sample")
    questions = sample_questions() if arguments.sample else load(arguments.questions)

    if arguments.sample:
        asyncio.run(_seed(settings, reset=arguments.reset))

    outcomes, facts = asyncio.run(
        evaluate(
            questions,
            arguments.top_k,
            reranker_name=settings.retrieval_reranker,
            min_score=settings.retrieval_min_score,
            fusion_k=(
                arguments.fusion_k
                if arguments.fusion_k is not None
                else settings.retrieval_fusion_k
            ),
            leg_limit=(
                arguments.leg_limit
                if arguments.leg_limit is not None
                else settings.retrieval_leg_limit
            ),
        )
    )
    scores = report(outcomes, arguments.top_k, facts)

    no_vectors = asyncio.run(missing_vectors(questions))
    if any(no_vectors.values()):
        print()
        print("children with no vector (the vector leg cannot reach these at all):")
        for title, count in sorted(no_vectors.items()):
            if count:
                print(f"  {count:>6}  {title}")

    if arguments.min_hit_rate is not None and scores["hybrid"].hit_rate < arguments.min_hit_rate:
        print(
            f"\nhybrid hit-rate {scores['hybrid'].hit_rate:.3f} is below "
            f"--min-hit-rate {arguments.min_hit_rate}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
