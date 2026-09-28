"""Offline retrieval evaluation: given questions and the documents that answer them, print
the hit rate — and say exactly what that number is a number *about*.

    docker compose exec -T api python /app/tests/tools/eval_retrieval.py questions.jsonl

    # against a scratch database, as the tests do
    docker compose exec -T -e TEST_DATABASE_NAME=eam_eval \
        api python /app/tests/tools/eval_retrieval.py questions.jsonl

Input is JSON Lines, one question per line — `questions.example.jsonl` beside this script is
a three-line one to copy:

    {"question": "¿Cuántos días de vacaciones?", "documents": ["Politica de vacaciones"]}
    {"question": "How many days of leave?", "documents": ["Politica", "Convenio"]}

`documents` are document **titles** (the `documents.title` column), not file names — a title
is what a citation shows and what a person writes when they build the fixture. A question may
name several documents, and any one of them counting as a hit is the usual reading.

## What it measures

For each question, the top `--top-k` child chunks by **cosine distance** against a vector
produced by the *configured* embedding provider, and:

* **hit@k** — the fraction of questions whose top-k chunks include a chunk of one of the
  expected documents. That is the whole metric. It is the metric ticket 33's hybrid retrieval
  has to beat, and it is deliberately not "did the answer contain the right sentence": that
  needs a reader, and a number nobody can interpret is worse than no number.
* **MRR** — the mean reciprocal rank of the first hit, printed beside it because hit@k hides
  whether the right document came first or fifth, and "fifth" is not good enough to put in
  front of a model with a small context window.
* **coverage** — how many questions had at least one expected document that is *indexed*: a
  document that is missing, not `ready`, or has no vectors is excluded from the denominator
  and reported separately. Without this the script would report a low hit rate for a corpus
  that simply has not been parsed yet, which is the one wrong answer that wastes an afternoon.

## What it does not measure

* **Not semantic quality, when the provider is the fake.** With `EMBEDDING_PROVIDER=fake` —
  the default outside production — the vectors are hashed bag-of-words, so "hit@5" is a
  *lexical* score: a question that shares no words with the right chunk will miss however good
  the retrieval is, and paraphrases are under-counted. The script prints the provider and the
  model it used on every run, and a number from the fake must not be compared with a number
  from `text-embedding-3-small`.
* **Not permissions.** It runs with `app.current_system` published, so it sees every document
  — including ones the question's asker could not read. That is right for measuring retrieval
  and wrong for measuring *leakage*; the leak test is ticket 30's, over the runtime role with
  a real caller context.
* **Not the pipeline.** It reads what is stored. A document that failed to parse, failed to
  embed, or was archived is counted as missing coverage rather than retried.
* **Not reranking, fusion or generation.** Ticket 33's RRF and rerank sit above this and are
  measured by their own comparison; this is the recall of the vector half alone.

Exit code is 0 whatever the hit rate: this is a measurement, not a check. A deployment that
runs it in CI wants `--min-hit-rate` (and gets a non-zero exit below it), which is the
threshold somebody has to choose deliberately rather than inherit.
"""

import argparse
import asyncio
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text

#: How many chunks the ranking returns per question. Five is the usual figure for a context
#: window that also has to hold a conversation, and it is the top-k the ticket's hybrids are
#: compared at.
DEFAULT_TOP_K = 5

#: The system flag the pipeline publishes. A measurement is not a request and has no caller,
#: so it declares itself the system — see `document.service.publish_system_context`. The
#: consequence is stated in the module docstring: this sees every document.
SYSTEM_SETTING = "app.current_system"


@dataclass(frozen=True, slots=True)
class Question:
    question: str
    documents: tuple[str, ...]
    line: int


@dataclass(frozen=True, slots=True)
class Outcome:
    question: Question
    ranked: list[str]
    hit_rank: int | None
    indexed: tuple[str, ...]
    missing: tuple[str, ...]


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


async def coverage(session, questions: list[Question]) -> dict[str, tuple[str, int]]:
    """Expected title → (document id, embedded child chunks), for the ones that are indexable.

    A title that is not in `documents`, or is not `ready`, or has no embedded child, is not
    in this mapping — and the caller reports those separately rather than counting them as
    misses.
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
                             WHERE c.document_id = d.id AND c.parent_chunk_id IS NOT NULL
                               AND c.embedding IS NOT NULL)
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


async def rank(session, embedder, question: str, top_k: int) -> list[str]:
    """The titles of the top-k child chunks for one question, best first.

    The vector search is the real one — `ORDER BY embedding <=> :probe`, which is the HNSW
    index's own query — and the title comes from the join rather than from a second lookup,
    so what is ranked and what is reported cannot disagree.
    """
    vectors = await embedder.embed([question])
    probe = "[" + ",".join(f"{value:.7f}" for value in vectors[0]) + "]"
    rows = (
        await session.execute(
            text(
                """
                SELECT d.title
                  FROM document_chunks c
                  JOIN documents d ON d.id = c.document_id
                 WHERE c.parent_chunk_id IS NOT NULL AND c.embedding IS NOT NULL
                 ORDER BY c.embedding <=> CAST(:probe AS vector)
                 LIMIT :k
                """
            ),
            {"probe": probe, "k": top_k},
        )
    ).all()
    return [row[0] for row in rows]


async def evaluate(questions: list[Question], top_k: int) -> tuple[list[Outcome], str, str]:
    """Run every question, and answer with the outcomes and what produced the vectors."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.config import get_settings
    from app.db import build_engine
    from app.domain.document.embeddings import build_embedder

    settings = get_settings()
    embedder = build_embedder(
        settings.embeddings_provider,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )
    if embedder is None:  # pragma: no cover - the operator asked for `none`
        raise SystemExit(
            "EMBEDDING_PROVIDER=none: there are no vectors to rank, so there is nothing to "
            "measure. Set it to `fake` for a lexical score or `openai` with a key."
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
            for question in questions:
                ranked = await rank(session, embedder, question.question, top_k)
                indexed = tuple(title for title in question.documents if title in available)
                missing = tuple(title for title in question.documents if title not in available)
                hit_rank = next(
                    (
                        position
                        for position, title in enumerate(ranked, start=1)
                        if title in indexed
                    ),
                    None,
                )
                outcomes.append(
                    Outcome(
                        question=question,
                        ranked=ranked,
                        hit_rank=hit_rank,
                        indexed=indexed,
                        missing=missing,
                    )
                )
    finally:
        await engine.dispose()
    return outcomes, embedder.name, settings.embeddings_provider


def report(outcomes: list[Outcome], top_k: int, model: str, provider: str) -> float:
    """Print the numbers, and answer with hit@k so a caller can threshold it."""
    scorable = [outcome for outcome in outcomes if outcome.indexed]
    hits = [outcome for outcome in scorable if outcome.hit_rank is not None]
    hit_rate = len(hits) / len(scorable) if scorable else 0.0
    mrr = (
        statistics.fmean(1.0 / outcome.hit_rank for outcome in hits) if hits else 0.0
    )

    print(f"provider: {provider}   model: {model}")
    if provider == "fake":
        print(
            "NOTE: the fake provider's vectors are hashed bag-of-words, so this is a\n"
            "      LEXICAL score. Do not compare it with a number from a real model."
        )
    print(f"questions: {len(outcomes)}   scored: {len(scorable)}   top-k: {top_k}")
    print(f"hit@{top_k}: {hit_rate:.3f}   MRR: {mrr:.3f}")
    print()

    for outcome in outcomes:
        if not outcome.indexed:
            expected = list(outcome.question.documents)
            print(f"  line {outcome.question.line}: NOT SCORED - none of {expected} is indexed")
            continue
        mark = "hit " if outcome.hit_rank is not None else "MISS"
        rank = f"@{outcome.hit_rank}" if outcome.hit_rank is not None else "   "
        print(f"  {mark} {rank} {outcome.question.question[:70]}")
        if outcome.hit_rank is None:
            print(f"        expected: {list(outcome.question.documents)}")
            print(f"        got:      {outcome.ranked}")
        if outcome.missing:
            print(f"        also expected, not indexed: {list(outcome.missing)}")
    return hit_rate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("questions", type=Path, help="JSON Lines: question + expected titles")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument(
        "--min-hit-rate",
        type=float,
        default=None,
        help="exit non-zero below this; unset means the run is a measurement, not a check",
    )
    arguments = parser.parse_args(argv)

    questions = load(arguments.questions)
    outcomes, model, provider = asyncio.run(evaluate(questions, arguments.top_k))
    hit_rate = report(outcomes, arguments.top_k, model, provider)

    if arguments.min_hit_rate is not None and hit_rate < arguments.min_hit_rate:
        print(f"\nbelow --min-hit-rate {arguments.min_hit_rate}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
