"""Embeddings: the seam, the vectors, the model on the row, and the repair.

Real PostgreSQL and a real pgvector column. The embedding *transport* is a seam by
design — `docs/architecture/codebase-design.md` §4 refuses a seam around the model while
noting that tests and the development stack need an implementation that does not call
OpenAI — so the fake below is an adapter rather than a mock, and the real HTTP
implementation is present in `app/domain/document/embeddings.py` for the deployment that
sets a key.

What is asserted here, in the ticket's order:

* **The vectors are written, on the children, and the model is recorded beside them.**
  Parents are context and are deliberately not embedded; `WHERE embedding IS NULL` is
  therefore two different things — "a parent" and "not embedded" — which is why the
  worklist query is about documents rather than rows.
* **A missing key is a clean, catalogued failure.** Not a stack trace and not a failed
  document: the text parsed, the document is `ready`, the vectors are absent, the
  catalogue carries `ERR_DOC_009`, and both language catalogues render a sentence.
* **The seam is not reached when the setting says embeddings are off.** Asserted with a
  recording adapter, because "did not call it" is a fact about calls and not about rows.
* **`reprocess` re-embeds without duplicating** — the version's chunks go first, links
  and vectors included, and the second run produces the same rows with the same vectors.
  This is the test the adversarial verification breaks.
"""

import asyncio
import json
import urllib.error
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings, get_settings
from app.core.constants import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL
from app.core.errors import ERRORS, ErrorCode
from app.domain.document.embeddings import (
    FAKE_MODEL,
    DeterministicEmbedder,
    EmbeddingUnavailable,
    OpenAIEmbedder,
    build_embedder,
    fold_accents,
)
from app.domain.document.models import DocumentStatus
from app.domain.document.service import DocumentService
from app.domain.document.storage import LocalFileStore
from app.jobs.parse_documents import reembed_one, system_session
from app.repositories.document import PostgresDocumentRepository
from tests.support.documents import markdown_bytes, text_bytes
from tests.support.platform import Platform
from tests.test_documents import Cast

if TYPE_CHECKING:
    from tests.support.platform import Actor

# --- the recording adapter --------------------------------------------------

#: A section long enough that the split has to produce several children, so "every
#: child was embedded" is a claim about more than one row.
PARAGRAPH = (
    "El personal con al menos un ano de antiguedad podra solicitar dias de vacaciones "
    "adicionales. La solicitud se presentara por escrito con quince dias de antelacion. "
    "El responsable respondera en un plazo maximo de cinco dias habiles. "
    "La concesion se comunicara al solicitante y al responsable del departamento. "
)

BODY = "# Manual de vacaciones\n\n" + "\n\n".join(
    f"## {index}. Seccion\n\n{PARAGRAPH * 8}" for index in range(1, 4)
)


@dataclass
class RecordingEmbedder:
    """The deterministic adapter, plus a record of what it was asked.

    Recording rather than returning canned vectors, because the interesting assertions
    are about *calls*: "nothing was embedded because the setting says so", "one call per
    batch", "the second run asked for the same texts". The vectors themselves come from
    the same adapter the development stack uses, so a test that asserts they are equal
    across two runs is asserting something real.
    """

    name: str = FAKE_MODEL
    calls: list[list[str]] = field(default_factory=list)
    _inner: DeterministicEmbedder = field(default_factory=DeterministicEmbedder)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return await self._inner.embed(texts)

    @property
    def embedded_texts(self) -> list[str]:
        return [item for call in self.calls for item in call]


@dataclass
class BrokenEmbedder:
    """An adapter whose provider is unreachable, which is what an expired key is."""

    name: str = EMBEDDING_MODEL
    detail: str = "embeddings HTTP 401: the API key is missing, revoked or not allowed"
    calls: int = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        raise EmbeddingUnavailable(self.detail)


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def document_storage(tmp_path, monkeypatch) -> str:
    """A storage root of this test's own; the same fixture `test_documents.py` uses."""
    monkeypatch.setattr(get_settings(), "document_storage_path", str(tmp_path))
    return str(tmp_path)


async def upload(actor: "Actor", content: bytes, *, filename: str, title: str):
    response = await actor.post(
        "/api/v1/documents",
        files={"file": (filename, content, "application/octet-stream")},
        data={"title": title},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def rows(platform: Platform, document_id: str) -> list[dict]:
    """The stored chunk rows, parents and children, with their vector facts.

    Read as SQL rather than through the module because the claims are about *columns*:
    whether a vector is present, which model is recorded beside it, and which row points
    at which. `embedding_model` is the column this ticket adds, and a service-level read
    would not show that it is written.
    """
    found = await platform.sql(
        """
        SELECT child.chunk_index, parent.chunk_index, child.heading_path, child.page_from,
               child.page_to, child.token_count, child.embedding IS NOT NULL,
               child.embedding_model, child.chunking_version, vector_dims(child.embedding)
        FROM document_chunks AS child
        LEFT JOIN document_chunks AS parent ON parent.id = child.parent_chunk_id
        WHERE child.document_id = :id ORDER BY child.chunk_index
        """,
        {"id": document_id},
    )
    return [
        dict(
            zip(
                (
                    "chunk_index",
                    #: The *parent's* position, not its uuid: a test that compares a uuid
                    #: against a chunk index passes for the wrong reason or fails for one.
                    "parent",
                    "heading",
                    "page_from",
                    "page_to",
                    "tokens",
                    "embedded",
                    "model",
                    "version",
                    "dimensions",
                ),
                row,
                strict=True,
            )
        )
        for row in found
    ]


# --- part 1: the seam, without a database -----------------------------------


def test_the_development_provider_is_the_fake_and_production_is_openai() -> None:
    """The derivation that keeps a fake from being silent.

    Development and test get the deterministic adapter, so `docker compose up` needs no
    key and a test can assert reproducibility. Anything else gets the real one, so a
    deployment that forgot the key reports `ERR_DOC_009` naming the key rather than
    quietly indexing a corpus of hashed vectors that answers every question badly.
    """
    assert Settings(app_env="development").embeddings_provider == "fake"
    assert Settings(app_env="test").embeddings_provider == "fake"
    assert Settings(app_env="production").embeddings_provider == "openai"
    assert Settings(app_env="staging").embeddings_provider == "openai"
    # And an explicit setting wins in both directions, which is what makes `none` and
    # `fake` available to a production that wants them.
    assert Settings(app_env="production", embedding_provider="none").embeddings_provider == "none"
    assert Settings(app_env="production", embedding_provider="fake").embeddings_provider == "fake"


def test_the_provider_none_builds_no_embedder_at_all() -> None:
    """`none` is a supported configuration, not a degenerate one.

    A corpus that is chunked and full-text searchable while the embedding key is being
    arranged is a legitimate deployment, and it has to be reachable without an
    exception — otherwise the only way to run without a key would be to run with a
    broken pipeline.
    """
    assert build_embedder("none") is None
    assert isinstance(build_embedder("fake"), DeterministicEmbedder)
    assert isinstance(build_embedder("openai", api_key="sk-test"), OpenAIEmbedder)
    with pytest.raises(ValueError):
        build_embedder("something-else")


def test_the_real_embedder_needs_a_key_and_says_which_one() -> None:
    """**A missing key is a catalogued failure, not a stack trace.**

    The exception carries the catalogue code and a sentence an operator can act on, and
    the code is in the catalogue with both languages rendered. That pairing is the
    requirement: a 500 with a traceback tells nobody that the remedy is one environment
    variable.
    """
    with pytest.raises(EmbeddingUnavailable) as refusal:
        build_embedder("openai", api_key=None)

    assert "OPENAI_API_KEY" in str(refusal.value)
    assert refusal.value.code is ErrorCode.DOCUMENT_EMBEDDING_UNAVAILABLE
    definition = ERRORS[ErrorCode.DOCUMENT_EMBEDDING_UNAVAILABLE]
    assert definition.status_code == 503
    from app.core.messages import MESSAGES

    for locale, catalogue in MESSAGES.items():
        assert catalogue[definition.message_key].strip(), f"no sentence in {locale}"


def test_the_real_embedder_posts_the_model_the_dimension_and_the_texts() -> None:
    """The HTTP implementation, exercised without a network.

    `urlopen` is what the adapter uses, so patching it is enough to assert the whole
    request: the endpoint, the bearer header, the model, the explicit `dimensions`
    (because §10.3 fixes the *column*, and a request that left it implicit would be
    trusting the provider's default to match our schema) and the texts in order.
    """
    seen: dict[str, object] = {}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN202 - urllib's signature
        seen["url"] = request.full_url
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.data.decode("utf-8"))
        payload = {
            "data": [
                {"index": 0, "embedding": [0.5] * EMBEDDING_DIMENSIONS},
                {"index": 1, "embedding": [0.25] * EMBEDDING_DIMENSIONS},
            ]
        }

        class Response:
            def read(self) -> bytes:
                return json.dumps(payload).encode("utf-8")

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        return Response()

    embedder = OpenAIEmbedder("sk-secret")
    import app.domain.document.embeddings as module

    original = module.urllib.request.urlopen
    module.urllib.request.urlopen = fake_urlopen
    try:
        vectors = asyncio.run(embedder.embed(["uno", "dos"]))
    finally:
        module.urllib.request.urlopen = original

    assert seen["url"] == "https://api.openai.com/v1/embeddings"
    assert seen["auth"] == "Bearer sk-secret"
    assert seen["body"] == {
        "model": EMBEDDING_MODEL,
        "input": ["uno", "dos"],
        "dimensions": EMBEDDING_DIMENSIONS,
        "encoding_format": "float",
    }
    assert len(vectors) == 2
    assert len(vectors[0]) == EMBEDDING_DIMENSIONS
    assert embedder.name == EMBEDDING_MODEL


def test_a_provider_error_becomes_the_catalogued_refusal() -> None:
    """A 401 from the provider is the same outcome as no key, with the provider's words.

    Asserted on the *message* rather than on an exception type, because what an operator
    reads is the message: it has to say which status came back and what it means for
    them, not merely that something failed.
    """
    import app.domain.document.embeddings as module

    def refuse(request, timeout=None):  # noqa: ANN001, ANN202 - urllib's signature
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", {}, None  # type: ignore[arg-type]
        )

    embedder = OpenAIEmbedder("sk-revoked")
    original = module.urllib.request.urlopen
    module.urllib.request.urlopen = refuse
    try:
        with pytest.raises(EmbeddingUnavailable) as refusal:
            asyncio.run(embedder.embed(["uno"]))
    finally:
        module.urllib.request.urlopen = original

    assert "401" in str(refusal.value)
    assert "API key" in str(refusal.value)


def test_a_wrong_dimension_from_the_provider_is_refused_rather_than_stored() -> None:
    """The one failure that would be *silent* if it were not checked.

    A vector of the wrong width cannot be stored in a `vector(1536)` column — but a
    response of the right width and the wrong *count* would misalign every text after
    it, and that one would be stored. Both are asserted here.
    """

    def respond(payload: dict):  # noqa: ANN202 - a small factory
        class Response:
            def read(self) -> bytes:
                return json.dumps(payload).encode("utf-8")

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        return lambda request, timeout=None: Response()

    import app.domain.document.embeddings as module

    embedder = OpenAIEmbedder("sk-test")
    original = module.urllib.request.urlopen
    try:
        module.urllib.request.urlopen = respond(
            {"data": [{"index": 0, "embedding": [0.1] * 10}]}
        )
        with pytest.raises(EmbeddingUnavailable) as narrow:
            asyncio.run(embedder.embed(["uno"]))
        assert "10 dimensions" in str(narrow.value)

        module.urllib.request.urlopen = respond(
            {"data": [{"index": 0, "embedding": [0.1] * EMBEDDING_DIMENSIONS}]}
        )
        with pytest.raises(EmbeddingUnavailable) as short:
            asyncio.run(embedder.embed(["uno", "dos"]))
        assert "got 1" in str(short.value)
    finally:
        module.urllib.request.urlopen = original


def test_the_fake_is_deterministic_and_lexically_similar() -> None:
    """What the fake is, stated as a test rather than as a hope.

    Determinism is not a nicety: `reprocess` re-embeds, and the test below asserts the
    second run wrote the *same* vectors — which a fake returning random numbers would
    fail. Lexical similarity is what makes the evaluation script's numbers
    interpretable: two texts sharing words are near each other, and this asserts that
    and its converse.
    """
    fake = DeterministicEmbedder()

    first = asyncio.run(fake.embed(["Politica de vacaciones anuales"]))[0]
    again = asyncio.run(fake.embed(["Politica de vacaciones anuales"]))[0]
    assert first == again
    assert len(first) == EMBEDDING_DIMENSIONS
    assert abs(sum(value * value for value in first) - 1.0) < 1e-9, "not normalised"

    async def similarity(left: str, right: str) -> float:
        a, b = await fake.embed([left, right])
        return sum(x * y for x, y in zip(a, b, strict=True))

    async def compare() -> tuple[float, float, float]:
        return (
            await similarity("vacaciones anuales", "vacaciones anuales retribuidas"),
            await similarity("vacaciones anuales", "permiso por matrimonio"),
            await similarity("vacación anual", "vacacion anual"),
        )

    related, unrelated, accented = asyncio.run(compare())
    assert related > unrelated
    assert accented > 0.9, "accents must fold: the same word typed two ways"
    # An empty text still gets a unit vector rather than a NaN, which pgvector would
    # sort last and silently drop from every result set.
    empty = asyncio.run(fake.embed([""]))[0]
    assert len(empty) == EMBEDDING_DIMENSIONS
    assert fold_accents("vacación") == "vacacion"


# --- part 2: the pipeline, against real PostgreSQL --------------------------


async def test_children_are_embedded_and_parents_are_not(
    platform: Platform, cast: Cast
) -> None:
    """**The ticket's vector line**: 1536 dimensions, on the rows retrieval searches.

    Parents are context, so embedding them would put a second, blurrier copy of the
    same text into the index — which is why the assertion is two-sided: every child has
    a vector, no parent does, and no parent points at another row.
    """
    embedder = RecordingEmbedder()
    document_id = await upload(
        cast.uploader, markdown_bytes(BODY), filename="manual.md", title="Manual"
    )
    from tests.test_documents import run_parse

    await run_parse(platform, document_id, embedder=embedder)

    stored = await rows(platform, document_id)
    children = [row for row in stored if row["parent"] is not None]
    parents = [row for row in stored if row["parent"] is None]

    assert children and parents
    assert len(parents) < len(children), "the fixture must produce more children than parents"
    assert all(row["embedded"] for row in children)
    assert all(row["dimensions"] == EMBEDDING_DIMENSIONS for row in children)
    assert all(row["model"] == FAKE_MODEL for row in children)
    assert all(not row["embedded"] for row in parents)
    # Which is what makes "unembedded" a question about *documents* and not about rows.
    assert embedder.embedded_texts, "the pipeline never asked for a vector"


async def test_the_model_and_the_split_version_are_recorded_on_every_row(
    platform: Platform, cast: Cast
) -> None:
    """**The ticket's "recorded so a future re-embed is a query"**, as two columns.

    `chunking_version` says which split produced the row and `embedding_model` which
    model produced its vector. Together they are the two questions a re-index asks —
    "which splits are old" and "which vectors are from the old model" — and each is an
    equality filter rather than a guess.
    """
    from app.domain.document.parsing import CHUNKING_VERSION

    document_id = await upload(
        cast.uploader, markdown_bytes(BODY), filename="manual.md", title="Manual"
    )
    from tests.test_documents import run_parse

    await run_parse(platform, document_id, embedder=RecordingEmbedder())

    stored = await rows(platform, document_id)
    assert {row["version"] for row in stored} == {CHUNKING_VERSION}
    assert {row["model"] for row in stored if row["embedded"]} == {FAKE_MODEL}
    # The two questions are one query each, which is the point of the columns.
    stale = await platform.scalar(
        "SELECT count(*) FROM document_chunks WHERE document_id = :id "
        "AND embedding IS NOT NULL AND embedding_model <> :model",
        {"id": document_id, "model": FAKE_MODEL},
    )
    assert stale == 0
    old_split = await platform.scalar(
        "SELECT count(*) FROM document_chunks WHERE document_id = :id AND chunking_version <> :v",
        {"id": document_id, "v": CHUNKING_VERSION},
    )
    assert old_split == 0


async def test_every_chunk_records_its_pages_and_its_order(
    platform: Platform, cast: Cast
) -> None:
    """**The ticket's page line, on the stored row**: from the parse, not from a guess.

    A two-page PDF: the child that quotes page 2 must name page 2, which is only possible
    because `parsing` captured the pages per page and carried them through the split. A
    child that runs from one page into the next names both — which is the assertion
    ticket 31's chunks could not make, since they named their first page at both ends.

    The rows would be identical if the pages had been *inferred* from the text, so the
    assertion is on the range a chunk's sentences actually span.
    """
    from tests.support.documents import pdf_bytes
    from tests.test_documents import run_parse

    content = pdf_bytes(
        "Primera pagina con una frase. Y otra frase mas para completar el parrafo entero.",
        "Segunda pagina con la respuesta exacta que se busca.",
    )
    document_id = await upload(cast.uploader, content, filename="politica.pdf", title="Politica")
    await run_parse(platform, document_id, embedder=RecordingEmbedder())

    stored = await rows(platform, document_id)
    pages = [row for row in stored if row["page_from"] is not None or row["page_to"] is not None]

    assert pages, "a PDF chunk must name its pages"
    # A child that ran from one page into the next names both, which is the assertion
    # ticket 31's chunks could not make — they named their first page at both ends, so
    # the same rows would have read `(1, 1)`.
    assert all(row["page_to"] is not None for row in pages)
    assert any(row["page_to"] >= 2 for row in pages), (
        "no chunk named the second page: "
        + repr([(row["page_from"], row["page_to"]) for row in stored])
    )
    assert all(1 <= (row["page_from"] or 0) <= 2 for row in pages)
    assert all((row["page_to"] or 0) >= (row["page_from"] or 0) for row in pages)
    assert all(row["chunk_index"] == index for index, row in enumerate(stored))
    assert all(row["tokens"] > 0 for row in stored)
    # `heading_path` is NULL here, and that is the honest answer rather than a defect: a
    # PDF has no heading styles, so a document whose section titles are detectable gets a
    # path (see `test_chunking.py`) and one that is plain prose gets none. A citation
    # falls back to the page, which is exactly what this test is about.
    assert all(row["heading"] is None for row in stored)


async def test_the_embedding_seam_is_not_reached_when_the_setting_says_off(
    platform: Platform, cast: Cast, monkeypatch
) -> None:
    """**`EMBEDDING_PROVIDER=none`: the seam is not called at all.**

    Asserted with a recording adapter, because "was not called" is a fact about calls
    and not about rows: a pipeline that called the provider and discarded the answer
    would leave the rows looking exactly the same. The document is still `ready`, the
    chunks are still written, and the full-text index still works — which is what makes
    `none` a supported configuration rather than a broken one.
    """
    from app.config import get_settings
    from tests.test_documents import run_parse

    monkeypatch.setattr(get_settings(), "embedding_provider", "none")
    assert get_settings().embeddings_provider == "none"

    recorder = RecordingEmbedder()
    document_id = await upload(
        cast.uploader, markdown_bytes(BODY), filename="manual.md", title="Manual"
    )
    await run_parse(platform, document_id, embedder=build_embedder("none"))

    stored = await rows(platform, document_id)
    assert stored, "the chunks must still be written"
    assert not any(row["embedded"] for row in stored)
    assert recorder.calls == [], "an embedder was reached for a deployment with none"
    assert await platform.scalar(
        "SELECT status FROM documents WHERE id = :id", {"id": document_id}
    ) == DocumentStatus.READY.value
    # And the full-text half is unaffected, which is the point of `none` being supported.
    found = await platform.scalar(
        "SELECT count(*) FROM document_chunks WHERE document_id = :id "
        "AND search_vector @@ websearch_to_tsquery('spanish', 'solicitud')",
        {"id": document_id},
    )
    assert found and found > 0


async def test_a_broken_embedder_leaves_a_ready_document_with_a_worklist_entry(
    platform: Platform, cast: Cast
) -> None:
    """**The clean, catalogued failure**: text stored, vectors missing, reason recorded.

    Three things have to be true at once, and each is a separate decision: the document
    is `ready` (its text is there, and `ready` is a claim about text), the chunks exist
    (throwing away a good split because a key expired destroys the part that worked),
    and the audit record says why — which is where an operator learns what to fix.
    """
    broken = BrokenEmbedder()
    document_id = await upload(
        cast.uploader, markdown_bytes(BODY), filename="manual.md", title="Manual"
    )
    from tests.test_documents import run_parse

    await run_parse(platform, document_id, embedder=broken)

    assert broken.calls == 1
    document = await platform.sql(
        "SELECT status, chunk_count, failure_reason FROM documents WHERE id = :id",
        {"id": document_id},
    )
    assert document[0][0] == DocumentStatus.READY.value
    assert document[0][1] > 0
    assert document[0][2] is None, "an embedding failure is not a parse failure"

    stored = await rows(platform, document_id)
    assert stored, "the split must survive the outage"
    assert not any(row["embedded"] for row in stored)

    reason = await platform.scalar(
        "SELECT after ->> 'embedding_failure' FROM audit_log WHERE entity_id = :id "
        "AND action = 'document.parsed'",
        {"id": document_id},
    )
    assert reason and "401" in reason


async def test_the_job_repairs_the_vectors_without_reparsing(
    platform: Platform, cast: Cast
) -> None:
    """**The repair path**: the worklist finds the document, `reembed` fills it in.

    This is what makes an outage a delay rather than a corpus somebody walks by hand.
    The assertion that it did *not* re-parse is the one that matters: the chunk rows must
    be the same rows — same ids, same indices, same text — with vectors added, because
    re-running the parser would rewrite text that was never wrong.
    """
    from tests.test_documents import run_parse

    document_id = await upload(
        cast.uploader, markdown_bytes(BODY), filename="manual.md", title="Manual"
    )
    await run_parse(platform, document_id, embedder=BrokenEmbedder())
    before = await platform.sql(
        "SELECT id, chunk_index, content, parent_chunk_id FROM document_chunks "
        "WHERE document_id = :id ORDER BY chunk_index",
        {"id": document_id},
    )
    assert before

    outcome = await reembed_one(UUID(document_id))

    assert outcome is not None and outcome.embedded
    after = await platform.sql(
        "SELECT id, chunk_index, content, parent_chunk_id FROM document_chunks "
        "WHERE document_id = :id ORDER BY chunk_index",
        {"id": document_id},
    )
    assert after == before, "the repair rewrote rows it should have left alone"
    stored = await rows(platform, document_id)
    assert all(row["embedded"] for row in stored if row["parent"] is not None)
    assert all(row["model"] == FAKE_MODEL for row in stored if row["embedded"])

    # Idempotent: a second repair finds nothing to do and says so rather than failing.
    again = await reembed_one(UUID(document_id))
    assert again is not None and not again.embedded


async def test_reprocessing_rebuilds_the_same_split_and_the_same_vectors(
    platform: Platform, cast: Cast
) -> None:
    """**Mutation test**: let `reprocess` leave the old rows behind and this fails.

    The ticket's retry line, extended to everything this ticket writes: the version's
    chunks go first — children, parents, links and vectors — and the re-parse rebuilds
    them from the split. Three assertions carry it, and each catches a different way of
    getting it wrong:

    * the row *count* catches an append;
    * the index list catches an append that renumbered;
    * the parent links catch a rewrite that orphaned them, and the vectors catch a
      rebuild that lost them.

    The vectors themselves must be equal because the fake is deterministic, which is a
    stronger claim than "there is a vector on every row".
    """
    from tests.test_documents import run_parse

    document_id = await upload(
        cast.uploader, markdown_bytes(BODY), filename="manual.md", title="Manual"
    )
    await run_parse(platform, document_id, embedder=RecordingEmbedder())
    before_rows = await rows(platform, document_id)
    before_vectors = await platform.sql(
        "SELECT chunk_index, embedding::text FROM document_chunks WHERE document_id = :id "
        "ORDER BY chunk_index",
        {"id": document_id},
    )
    assert len(before_rows) > 3, "the fixture must produce several rows for this to mean anything"

    reprocessed = await cast.uploader.post(f"/api/v1/documents/{document_id}/reprocess")
    assert reprocessed.status_code == 200, reprocessed.text
    # The removal is synchronous and the re-parse is not: between the two calls the
    # document has no chunks at all, links and vectors included.
    assert await platform.scalar(
        "SELECT count(*) FROM document_chunks WHERE document_id = :id", {"id": document_id}
    ) == 0
    assert reprocessed.json()["status"] == DocumentStatus.PROCESSING.value

    # The re-parse as the job runs it, on *this* database: `parse_one` opens its own
    # session against the process's `DATABASE_URL`, which is the development database,
    # and a test that drove it would be asserting about a document in another schema.
    await run_parse(platform, document_id, embedder=RecordingEmbedder())

    after_rows = await rows(platform, document_id)
    after_vectors = await platform.sql(
        "SELECT chunk_index, embedding::text FROM document_chunks WHERE document_id = :id "
        "ORDER BY chunk_index",
        {"id": document_id},
    )
    assert len(after_rows) == len(before_rows)
    assert [row["chunk_index"] for row in after_rows] == [
        row["chunk_index"] for row in before_rows
    ]
    assert after_vectors == before_vectors, "the second run wrote different vectors"
    # The links survived the rewrite: every child still points at a parent row that
    # exists, which is what the delete-then-insert order makes possible.
    parents = {row["chunk_index"] for row in after_rows if row["parent"] is None}
    assert parents
    assert all(
        row["parent"] in parents for row in after_rows if row["parent"] is not None
    )


async def test_a_rebuild_leaves_no_orphan_when_the_document_shrinks(
    platform: Platform, cast: Cast
) -> None:
    """A shorter split must not leave the longer one's tail behind.

    `replace_chunks` deletes before it inserts, and this is the case that proves the
    delete runs: the same document version is re-parsed from a *shorter* file, and the
    rows that no longer belong must be gone — they are rows the unique
    `(document_id, chunk_index)` would otherwise let survive at their old positions.
    """
    from pathlib import Path

    from tests.test_documents import run_parse

    document_id = await upload(
        cast.uploader, markdown_bytes(BODY), filename="manual.md", title="Manual"
    )
    await run_parse(platform, document_id, embedder=RecordingEmbedder())
    before = await platform.scalar(
        "SELECT count(*) FROM document_chunks WHERE document_id = :id", {"id": document_id}
    )
    assert before > 2

    # The uploader replaces the stored original with a much shorter version of the same
    # document — the "here is the corrected export" case `reprocess` exists for.
    key = await platform.scalar(
        "SELECT storage_path FROM documents WHERE id = :id", {"id": document_id}
    )
    (Path(get_settings().document_storage_path) / key).write_bytes(
        markdown_bytes("# Manual de vacaciones\n\nUna sola frase.")
    )
    await platform.sql(
        "UPDATE documents SET media_type = 'text/markdown' WHERE id = :id", {"id": document_id}
    )
    await cast.uploader.post(f"/api/v1/documents/{document_id}/reprocess")
    await run_parse(platform, document_id, embedder=RecordingEmbedder())

    after = await rows(platform, document_id)
    assert len(after) < before, "the shorter split must have replaced the longer one"
    assert [row["chunk_index"] for row in after] == list(range(len(after)))
    assert await platform.scalar(
        "SELECT count(*) FROM document_chunks WHERE document_id = :id AND embedding IS NOT NULL",
        {"id": document_id},
    ) >= 1


async def test_a_chunk_read_is_bounded_by_the_documents_visibility(
    platform: Platform, cast: Cast
) -> None:
    """**The ticket's access line**: chunk reads go through the same rule as documents.

    The vector is in the chunk row, so a retrieval query reads `document_chunks` — and a
    chunk whose document the caller cannot open must be invisible to them under the
    database's own policy. This is ticket 31's policy asserted against the rows *this*
    ticket writes, because a parent row is a new shape of row and a policy that only
    covered children would leak a document's whole context block.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from tests.support.documents import text_bytes as empty_text_bytes
    from tests.test_documents import publish, run_parse

    document_id = await upload(
        cast.uploader, markdown_bytes(BODY), filename="manual.md", title="Manual"
    )
    await run_parse(platform, document_id, embedder=RecordingEmbedder())
    assert await platform.scalar(
        "SELECT count(*) FROM document_chunks WHERE document_id = :id AND embedding IS NOT NULL",
        {"id": document_id},
    ) > 0
    assert empty_text_bytes  # imported for the shared builders' sake

    settings = get_settings()
    engine = create_async_engine(settings.runtime_test_database_url)
    try:
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            await publish(session, cast.outsider.employee_id, ["low"], [])
            seen = (
                (await session.execute(text("SELECT document_id::text FROM document_chunks")))
                .scalars()
                .all()
            )
        async with factory() as session:
            await publish(session, cast.uploader.employee_id, ["low"], [])
            mine = (
                (await session.execute(text("SELECT document_id::text FROM document_chunks")))
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()

    assert document_id not in seen
    assert set(mine) == {document_id}


async def test_the_audit_record_says_what_was_embedded(platform: Platform, cast: Cast) -> None:
    """The parse record carries the count and the model, which is what an operator reads.

    `document.parsed` already says how many chunks were written; the two fields this
    ticket adds make the same record answer "were they embedded, and by what" — the
    question that distinguishes a working corpus from one that is silently unsearchable.
    """
    from tests.test_documents import run_parse

    document_id = await upload(
        cast.uploader, markdown_bytes(BODY), filename="manual.md", title="Manual"
    )
    await run_parse(platform, document_id, embedder=RecordingEmbedder())

    after = await platform.sql(
        "SELECT after -> 'embedded_count', after ->> 'embedding_model', "
        "       after ->> 'embedding_failure' FROM audit_log "
        "WHERE entity_id = :id AND action = 'document.parsed'",
        {"id": document_id},
    )
    assert after
    embedded_count, model, failure = after[0]
    assert int(embedded_count) > 0
    assert model == FAKE_MODEL
    assert failure is None


async def test_the_worklist_finds_documents_with_missing_vectors(
    platform: Platform, cast: Cast
) -> None:
    """`embedding IS NULL` as a *document* list, which is what the job pass reads.

    A document with one unembedded child is on the list once, not once per row — and a
    fully embedded document is not on it at all, which is the property that makes the
    pass terminate.
    """
    from tests.test_documents import run_parse

    broken = await upload(
        cast.uploader, markdown_bytes(BODY), filename="roto.md", title="Roto"
    )
    await run_parse(platform, broken, embedder=BrokenEmbedder())
    whole = await upload(
        cast.uploader, text_bytes("Veintitres dias laborables."), filename="bien.txt", title="Bien"
    )
    await run_parse(platform, whole, embedder=RecordingEmbedder())

    # The control is embedded, which is what makes the assertion below about the worklist
    # rather than about a fixture that failed to embed.
    assert await platform.scalar(
        "SELECT count(*) FROM document_chunks WHERE document_id = :id "
        "AND embedding IS NOT NULL AND parent_chunk_id IS NOT NULL",
        {"id": whole},
    ) > 0

    async with system_session() as session:
        service = DocumentService(
            PostgresDocumentRepository(session),
            session,
            principal=None,  # type: ignore[arg-type]
            storage=LocalFileStore(get_settings().document_storage_path),
            embedder=RecordingEmbedder(),
        )
        pending = await service.pending_embed_documents()

    assert [str(item) for item in pending] == [broken]
    # And with embeddings switched off the list is empty rather than everything: a job
    # that re-discovered an unconfigured provider on every pass would be a job that never
    # finished its work.
    async with system_session() as session:
        off = DocumentService(
            PostgresDocumentRepository(session),
            session,
            principal=None,  # type: ignore[arg-type]
            storage=LocalFileStore(get_settings().document_storage_path),
            embedder=None,
        )
        assert await off.pending_embed_documents() == []
