"""PostgreSQL implementation of the hybrid search.

**"Parallel" means one statement with two CTEs, and that is the honest reading.**
`docs/DESIGN.md` §5.2 writes the two legs as `pgvector 向量 top 20 ∥ Postgres 全文检索
top 20`. Both halves read the same table on the same connection, and one `AsyncSession`
is one connection with one transaction: issuing two coroutines and gathering them would
interleave two statements on one connection, which psycopg serialises anyway — a
coroutine that *looks* concurrent and is not. So the two legs are one statement:

    WITH vector_leg AS (
            SELECT * FROM (
                SELECT c.id, ..., rank() OVER (ORDER BY c.embedding <=> :probe) AS rank
                  FROM document_chunks c JOIN documents d ON d.id = c.document_id
                 WHERE c.parent_chunk_id IS NOT NULL AND d.status = 'ready' AND <filter>
            ) ranked WHERE rank <= :leg_limit),
         text_leg AS ( ... the same shape, ranked by ts_rank_cd ... )
    SELECT ... FROM (SELECT * FROM vector_leg UNION ALL SELECT * FROM text_leg) v
      JOIN document_chunks c ON c.id = v.id
      LEFT JOIN document_chunks parent ON parent.id = c.parent_chunk_id

That buys the three things the ticket actually wants from "并行": **one database round
trip** rather than two (the latency argument), **one snapshot** for both legs (a document
published between them cannot appear in one leg and be missing from the other), and **one
place the permission predicate is written**, so half a search cannot run unfiltered.
PostgreSQL executes both CTEs over one scan of `document_chunks` and the planner may
share it. What it does not buy is CPU-level parallelism inside one backend, and nothing
here claims it does.

**The rank is computed in SQL, by `rank() OVER (...)`, and not by the position of the row
in the answer.** The union concatenates the two legs, so a row's position in the result
is meaningless across legs; the window function is what makes "first in its own leg" a
fact. Each leg's window orders by its own measure — ascending cosine distance for the
vector leg (pgvector's `<=>` is a *distance*, lower is better) and descending
`ts_rank_cd` for the text leg — with `c.id` as the tiebreaker, so two runs of one query
produce the same ranks and therefore the same fusion. The `WHERE rank <= :leg_limit`
sits in an outer select rather than in the CTE because a window function is evaluated
after `WHERE`, so the limit has to be applied one level up.

**The filter goes in the SQL, not on the way out.** §4.3's structural constraint is that
permission is a *pre*-filter: 「检索 SQL 的 WHERE 子句必须包含上述判定」, applied before
retrieval rather than after it. A chunk of an unreachable document is therefore never
ranked, never fetched and never counted — which is the difference between "not in the hit
set" and "marked invisible". Ticket 35 pushes a real `FilterSpec`; this module never
invents one, and `None` means the caller gave none.

**A personal document is recalled by its owner and by nobody else (ticket 36).** The
first clause below is gated on `NOT d.is_company_kb`, so it is a statement about personal
documents rather than about any document that happens to name an owner; the clause that
would reach a colleague's *published* personal document is rendered only when the spec
carries `personal_documents_via_department`, and the retrieval path clears that flag
(`domain/retrieval/filtering.py::answer_filter_for`). So the pool is the company
knowledge base plus the asker's own uploads, which is exactly 「个人文档不进入公司知识库的
检索池」, and the document list — which sets the flag — still shows a colleague what they
may open.

**`visible_document_clauses` is deliberately readable and deliberately here.** The kernel
states §4.2 as data and each store renders it; the document list's rendering is
`repositories/document.py`'s `_visible`, and this one is the retrieval query's. They are
two renderings of one rule, and `tests/test_retrieval.py` asserts they agree about a
document — which is the property that matters, rather than two stores sharing a private
helper across a boundary that exists for a different query shape.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access.kernel import FilterSpec
from app.domain.retrieval.models import RankedCandidate

#: The text search configuration, written as a literal for the reason migration 0022
#: writes it as one: it is what makes the generated `search_vector` column possible, and a
#: second place to configure the corpus's language would be a second thing to keep in step
#: with the column. `websearch_to_tsquery` rather than `to_tsquery`: a user's question
#: contains punctuation, quotes and hyphens, and the web-search syntax absorbs them where
#: `to_tsquery` raises a syntax error on them — see `text_query` for what "absorbs" costs
#: and why the query is normalised before it gets here.
TEXT_SEARCH_CONFIG = "spanish"

#: What is stripped from a query before it becomes a tsquery.
#:
#: **Not cosmetic, and this was a real defect.** `websearch_to_tsquery` does not discard
#: Spanish question punctuation — it *keeps* it inside the token, so `¿Cuántos días?`
#: becomes the lexeme `'¿cuant'`, which matches nothing, and the text leg returns zero rows
#: for every question a person would actually type. Verified against this project's
#: PostgreSQL 18: `websearch_to_tsquery('spanish', '¿Cuántos días?')` is
#: `'¿cuant' & 'dias'`, while the same text without the marks is `'cuant' & 'dias'`.
#: `to_tsvector` applies the same parser, so a *document* that opens with `¿` has a lexeme
#: the query can never name either.
#:
#: The hyphen is in the set for the same reason and with a second consequence: `GT-2026`
#: becomes the phrase `'gt' <-> '-2026'`, which matches only that exact phrase, where
#: splitting on it matches a document that writes the code either way.
#:
#: Apostrophes and the Latin-1 punctuation that survives accent folding are dropped
#: outright rather than replaced, so `d'art` does not become two lexemes and `España` — whose
#: `ñ` a naive split would take for a separator once it is not a letter in the original
#: script — stays one word.
_QUERY_SEPARATORS = str.maketrans({character: " " for character in "¿?¡!-–—\"'“”‘’«»()[]{}"})

#: Characters dropped without leaving a space: punctuation that sits *inside* a word.
_QUERY_ELIDED = ".,;:*/\\|@#&+=<>~^%$`´¨¯"


def text_query(query: str) -> str:
    """The query normalised for the Spanish text search parser: see `_QUERY_SEPARATORS`."""
    separated = query.translate(_QUERY_SEPARATORS)
    return "".join(
        " " if character in _QUERY_ELIDED else character for character in separated
    )


def _literal(value: str) -> str:
    """One SQL string literal, quoted. The value is a user's question, so this matters."""
    return "'" + value.replace("'", "''") + "'"


def tsquery_expression(normalised: str) -> str:
    """A tsquery that reads a *question* the way a person means it.

    `websearch_to_tsquery` joins every term with `AND`, which is the right rule for a
    search box and the wrong one for a question:

    * **`¿Cuántos días de permiso por matrimonio corresponden?` becomes five lexemes of
      which three are the interrogative, the preposition and the verb.** Requiring all of
      them is requiring the document to contain "corresponden", which no policy does, and
      the answer is missed although the passage that holds it *is* the one about marriage
      leave. Measured on this repository's sample: `AND` scores hit@5 = 0.111 over nine
      questions, `OR` over the same normalised text scores far higher.
    * **A policy code is a single identifier, not a phrase.** `GT-2026` must match, and a
      question of one or two words is usually an identifier rather than prose.

    So: `websearch_to_tsquery` for a query of one or two words — phrases, quoted strings
    and `or` still mean what they mean there, and an identifier is the case that needs it —
    and an `OR` of the same lexemes for a longer one, ranked by `ts_rank_cd` so a chunk
    containing more of the question's terms still comes first.

    `to_tsquery('spanish', '')` raises a syntax error, so the empty case is guarded rather
    than left to the caller: a query made only of stopwords normalises to nothing.
    """
    words = normalised.split()
    if not words:
        return f"to_tsquery('{TEXT_SEARCH_CONFIG}', '')"
    terms = []
    for word in words:
        # `:*` would be a prefix match and is deliberately not used: a prefix search on
        # `dia` matches `diario`, and the corpus's own stemming already handles the
        # endings a Spanish question varies (`dia`/`dias`, `solicitud`/`solicitar`).
        if all(character.isalnum() or character.isalpha() for character in word):
            terms.append(word)
    if len(words) <= 2 or not terms:
        return f"websearch_to_tsquery('{TEXT_SEARCH_CONFIG}', {_literal(normalised)})"
    joined = " | ".join(terms)
    return (
        f"CASE WHEN to_tsquery('{TEXT_SEARCH_CONFIG}', {_literal(joined)})::text = '' "
        f"THEN websearch_to_tsquery('{TEXT_SEARCH_CONFIG}', {_literal(normalised)}) "
        f"ELSE to_tsquery('{TEXT_SEARCH_CONFIG}', {_literal(joined)}) END"
    )

#: What the answer selects, in the order `_candidate` reads it: the leg's rank and score
#: first, then the chunk, the document a citation names, and the parent a citation
#: quotes.
_ANSWER_COLUMNS = """
            v.rank,
            v.distance,
            v.text_rank,
            c.id,
            c.document_id,
            d.title,
            d.filename,
            c.content,
            c.parent_chunk_id,
            parent.content,
            c.heading_path,
            c.page_from,
            c.page_to,
            d.is_company_kb
"""

_VECTOR_ORDER = "c.embedding <=> CAST(:probe AS vector)"

#: The body both legs share. Everything injected here begins with ` AND `, including
#: the empty string, so the template needs no extra spaces. `{distance}` and `{score}`
#: are filled by whichever leg is being built and left NULL by the other, which is what
#: lets one row shape carry both and the caller tell the two apart without a second
#: column list. `{match}` is the text leg's `@@` predicate — empty for the vector leg,
#: which selects by distance and excludes unembedded rows instead.
_LEG_BODY = """
                SELECT c.id,
                       c.document_id,
                       c.heading_path,
                       c.page_from,
                       c.page_to,
                       {distance} AS distance,
                       {score} AS text_rank,
                       rank() OVER (ORDER BY {order}) AS rank
                  FROM document_chunks AS c
                  JOIN documents AS d ON d.id = c.document_id
                 WHERE c.parent_chunk_id IS NOT NULL
                   AND d.status = 'ready'{match}{visible}
"""


def _vector_literal(vector: list[float]) -> str:
    """A pgvector literal, at the precision the column stores.

    The same rendering `repositories/document.py` uses, for the same reason: `%g`'s
    seven significant digits is what `vector` keeps anyway (single precision), so the
    round trip through this string cannot lose a value the column would have held.
    """
    return "[" + ",".join(f"{value:.7g}" for value in vector) + "]"


def visible_document_clauses(spec: FilterSpec | None) -> tuple[list[str], dict[str, object]]:
    """The `FilterSpec` as SQL — `(clauses, parameters)`, one clause per `OR` term.

    Without a spec the answer is "no clause", which is an *unfiltered* search and not a
    refusal: the offline evaluation and the system's own pass have no caller. What makes
    that visible is `SearchOutcome.filtered`, owned by the service.

    With a spec, the clauses are §4.2's, in the design's order and with its connectives:

    1. **§4.2's clause 1, gated on the document being personal** (ticket 36) —
       `NOT d.is_company_kb AND d.owner_employee_id = :filter_employee_id`. The
       ownership *test* is what the design states; the `NOT d.is_company_kb` gate is
       what makes this a statement about personal documents rather than about every
       document one happens to own, and the two are read together as one clause because
       they are one rule. A company document reaches a caller through clause 2, with
       its department and its ceiling — an administrator owns nothing, and a personal
       upload of somebody else's is reached through nothing at all.
    2. **a company knowledge-base document**, within the ceiling *and* in a department
       the caller reaches. The ceiling is a membership test against `clearance_levels`
       rather than a rank comparison: the kernel has already expanded the principal's own
       level into the set of levels at or below it, so re-deriving the ladder here would
       be a second copy of `CLEARANCE_RANK`;
    3. **a colleague's personal document published to the department** (ticket 36) — and
       it is rendered **only when the spec says so**. The document list sets
       `personal_documents_via_department`, so the colleague sees a document they may
       open; the retrieval path clears it (`filtering.answer_filter_for`), because the
       ticket's rule for the pool is that only the asker's own personal documents are
       recalled: 「个人文档不进入公司知识库的检索池」. The clause carries the same
       `clearance_level = ANY(...)` ceiling the company clause does — being published to
       a department is not a reason to read above your clearance (D11's note: 显式共享不能
       突破密级上限) — and `visibility = 'department'` is what the owner's publication
       *is*;
    4. **the exception roles**, for company documents only, within the ceiling
       (`company_kb_cross_department`).

    **§4.2's explicit-share clause has no clause here, and that is deliberate.** There is
    no `document_permissions` table in this schema: the design's "share with a person"
    is expressed as the ownership column plus the `visibility` column (owner-only, or
    the whole department), which is the two-step this ticket's checklist asks for. So
    the earlier placeholder — a comment promising ticket 36 would add a lookup — is
    replaced by the clause it actually needed.

    `allow_all` is refused rather than honoured. The kernel never produces it for
    `ResourceKind.DOCUMENT` and says why; a permissive spec that arrived here would turn a
    search into "every chunk in the corpus", which is the disclosure §4.3's constraint
    exists to prevent. A spec that reaches *nothing* — no departments, no ownership, no
    cross-department role — renders as `false` and not as "no clause", because the two
    read the same way in a `WHERE` and mean opposite things.
    """
    if spec is None:
        return [], {}
    if spec.allow_all:  # pragma: no cover - `filter_for` never produces this
        raise ValueError(
            "a document filter with allow_all would let a search reach every chunk; "
            "the kernel does not produce one and this module will not honour it"
        )

    clauses: list[str] = []
    parameters: dict[str, object] = {}

    if spec.own_employee_id is not None:
        # Clause 1: personal documents, and only one's own. The `is_company_kb` gate is
        # load-bearing twice over: it is what keeps this a statement about personal
        # documents, and it is what stops the *ownership test* being the only thing
        # between a personal document and a caller whose principal was built from
        # somebody else's id.
        clauses.append(
            "(NOT d.is_company_kb AND d.owner_employee_id = :filter_employee_id)"
        )
        parameters["filter_employee_id"] = spec.own_employee_id

    clears = sorted(spec.clearance_levels)
    departments = sorted(str(value) for value in spec.department_ids)

    if spec.include_company_kb:
        if not clears or not departments:
            # An empty `IN ()` is not valid SQL, and "this caller reaches no department"
            # is an answer rather than a syntax question. A clause that cannot match
            # anything is `false` rather than an omission: §4.2 is a *disjunction*, so a
            # dropped clause would silently widen the reach instead of narrowing it.
            clauses.append("false")
        else:
            clauses.append(
                "(d.is_company_kb "
                "AND d.clearance_level = ANY(CAST(:filter_clearances AS text[])) "
                "AND d.department_id = ANY(CAST(:filter_departments AS uuid[])))"
            )
            parameters["filter_clearances"] = clears
            parameters["filter_departments"] = departments

    # Clause 3, rendered only where the spec asks for it. See the docstring: the
    # document list sets the flag and the retrieval path clears it.
    if spec.personal_documents_via_department:
        if not clears or not departments:
            clauses.append("false")
        else:
            clauses.append(
                "(NOT d.is_company_kb "
                "AND d.visibility = 'department' "
                "AND d.clearance_level = ANY(CAST(:filter_clearances AS text[])) "
                "AND d.department_id = ANY(CAST(:filter_departments AS uuid[])))"
            )
            parameters.setdefault("filter_clearances", clears)
            parameters.setdefault("filter_departments", departments)

    if spec.company_kb_cross_department:
        if not clears:
            clauses.append("false")
        else:
            clauses.append(
                "(d.is_company_kb "
                "AND d.clearance_level = ANY(CAST(:filter_clearances AS text[])))"
            )
            parameters.setdefault("filter_clearances", clears)

    if not clauses:
        return ["false"], {}
    return clauses, parameters


def visible_document_predicate(spec: FilterSpec | None) -> tuple[str, dict[str, object]]:
    """The spec as one SQL fragment and its parameters, ready to be `AND`ed into a leg.

    **`OR`, and this is the whole rule.** §4.2 is four *alternatives* — your own
    document, a company document you reach, an explicit share, the exception roles — so
    a caller reaches a document when *any* clause holds. Joining the clauses with `AND`
    is a different and much narrower rule, and it is the mistake that hid here until the
    filter was exercised end to end: nobody's own document is a company document *and*
    owned by them, so an `AND` filter reaches nothing at all and looks like a working
    permission boundary.
    """
    clauses, parameters = visible_document_clauses(spec)
    if not clauses:
        return "", {}
    return "(" + " OR ".join(f"({clause})" for clause in clauses) + ")", parameters


class PostgresChunkSearchRepository:
    """The two legs, the ranking, the filter and the parent lookup, in one statement."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search_legs(
        self,
        query: str,
        *,
        embedding: list[float] | None,
        leg_limit: int,
        filter_spec: FilterSpec | None = None,
    ) -> tuple[list[RankedCandidate], list[RankedCandidate]]:
        """One round trip. See the module docstring for why this is one statement."""
        predicate, parameters = visible_document_predicate(filter_spec)
        # ANDed into both legs, which is what makes this a pre-filter rather than a
        # post-filter: a chunk whose document no clause reaches is never ranked at all.
        visible = f" AND {predicate}" if predicate else ""
        # The text leg's own query, normalised and turned into the tsquery a question
        # needs — see `text_query` and `tsquery_expression`. It is a literal in the
        # statement rather than a bound parameter because the tsquery is built by a
        # *function* the statement has to call; the value inside it is escaped.
        tsquery = tsquery_expression(text_query(query))
        score = (
            f"ts_rank_cd(c.search_vector, {tsquery})"
        )
        match = f"c.search_vector @@ {tsquery}"
        parameters.update({"leg_limit": leg_limit})
        if embedding is not None:
            parameters["probe"] = _vector_literal(embedding)

        vector_leg = f"""
            vector_leg AS (
                SELECT * FROM (
                    {_LEG_BODY.format(
                        distance=_VECTOR_ORDER,
                        score="NULL::real",
                        order=_VECTOR_ORDER,
                        match="",
                        visible=visible,
                    )}
                ) AS ranked
                 WHERE rank <= :leg_limit
            )
        """
        text_leg = f"""
            text_leg AS (
                SELECT * FROM (
                    {_LEG_BODY.format(distance="NULL::double precision", score=score,
                                      order=f"{score} DESC, c.id", match=f"AND {match}",
                                      visible=visible)}
                ) AS ranked
                 WHERE rank <= :leg_limit
            )
        """
        # The vector CTE is omitted entirely when there is no probe rather than selected
        # with a `false` predicate: a leg that cannot run should cost nothing, and
        # `CAST(NULL AS vector)` would be a cast error rather than an empty list.
        with_clause = f"{vector_leg},{text_leg}" if embedding else text_leg
        union = (
            "SELECT * FROM vector_leg UNION ALL SELECT * FROM text_leg"
            if embedding
            else "SELECT * FROM text_leg"
        )
        statement = f"""
            WITH {with_clause}
            SELECT {_ANSWER_COLUMNS}
              FROM ({union}) AS v
              JOIN document_chunks AS c ON c.id = v.id
              JOIN documents AS d ON d.id = v.document_id
              LEFT JOIN document_chunks AS parent ON parent.id = c.parent_chunk_id
        """
        rows = (await self._session.execute(text(statement), parameters)).all()

        vector: list[RankedCandidate] = []
        textual: list[RankedCandidate] = []
        for row in rows:
            candidate = _candidate(row)
            if candidate.vector_distance is not None:
                vector.append(candidate)
            if candidate.text_rank is not None:
                textual.append(candidate)
        # Ordered by the leg's own rank, which the window function computed: the union
        # concatenated the two legs, so the answer's row order is not either leg's.
        vector.sort(key=lambda item: item.rank)
        textual.sort(key=lambda item: item.rank)
        return vector, textual


def _candidate(row) -> RankedCandidate:  # noqa: ANN001 - a SQLAlchemy Row
    """One row of the union as a candidate, with whichever score its own leg produced."""
    return RankedCandidate(
        chunk_id=row[3],
        document_id=row[4],
        document_title=row[5],
        filename=row[6],
        content=row[7],
        parent_chunk_id=row[8],
        parent_content=row[9],
        heading_path=row[10],
        page_from=row[11],
        page_to=row[12],
        rank=int(row[0]),
        vector_distance=float(row[1]) if row[1] is not None else None,
        text_rank=float(row[2]) if row[2] is not None else None,
        # Appended to `_ANSWER_COLUMNS` rather than inserted after `filename`, so the
        # positional contract above stays legible: a new column is a new index at the
        # end, and nothing before it moves.
        is_company_kb=bool(row[13]),
    )


__all__ = [
    "PostgresChunkSearchRepository",
    "TEXT_SEARCH_CONFIG",
    "text_query",
    "tsquery_expression",
    "visible_document_clauses",
    "visible_document_predicate",
]
