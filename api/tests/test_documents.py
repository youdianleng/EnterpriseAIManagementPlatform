"""Document upload and the asynchronous parsing pipeline (ticket 31).

Real PostgreSQL, real Redis, a real parser, and a real file on disk. No mocks, for the
reason the rest of the suite gives: what this ticket has to get right is a statement
about *rows and bytes* — a document that is one row per file, a scanned PDF that must
not become a ready document, a retry that must not double the chunks, a row that must be
invisible to another department even when a query forgets its filter — and a substitute
would answer with the test's own assumptions about all of them.

Every test names the checklist line it pins. The nine that matter most:

* `test_an_unsupported_type_is_refused_with_a_bilingual_message` — the five accepted
  formats, and the readable refusal for everything else.
* `test_a_file_over_the_ceiling_is_refused_not_truncated` — 50 MB, refused.
* `test_a_crafted_filename_cannot_escape_the_storage_directory` — `../`, normalised.
* `test_upload_answers_before_parsing_and_leaves_the_document_processing` — the HTTP
  request does not wait.
* `test_the_job_takes_a_processing_document_to_ready` — and the background half does.
* `test_a_scanned_pdf_fails_with_the_message_the_ticket_words` — no OCR, ever.
* `test_the_original_is_kept_and_downloadable_under_the_read_rule` — the file survives,
  and the permission is the *read's*, not a second rule.
* `test_uploading_the_same_file_twice_is_a_conflict_naming_the_first` — content hash,
  not a silent second document.
* `test_reprocessing_produces_no_duplicate_chunks` — **the retry is idempotent**, and a
  failed parse can be retried while a successful one re-runs unchanged.
* `test_a_document_outside_the_callers_department_is_invisible_to_the_database` — the
  ticket 13 predicate, attached and proved over the restricted role.
"""

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.audit import AuditAction
from app.config import Settings
from app.core.constants import EMBEDDING_DIMENSIONS
from app.core.errors import ErrorCode
from app.domain.document.files import FALLBACK_NAME, MAX_UPLOAD_BYTES
from app.domain.document.models import DocumentStatus
from app.domain.document.parsing import NO_TEXT_MESSAGE
from app.domain.document.service import DocumentService
from app.jobs.parse_documents import parse_one, parse_pending
from app.repositories.document import PostgresDocumentRepository

#: Re-exported so the document modules that came after this one can name the cast without
#: importing this test module — see `conftest.py`, which owns the fixture.
from tests.conftest import Cast
from tests.support.documents import (
    RecordingFileStore,
    docx_bytes,
    empty_xlsx_bytes,
    markdown_bytes,
    pdf_bytes,
    scanned_pdf_bytes,
    text_bytes,
    xlsx_bytes,
)
from tests.support.platform import Actor, Platform

#: The role requests connect as. Named rather than imported from the migration, so a
#: rename shows up as a failing test rather than as two places agreeing.
APP_ROLE = "eam_app"


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def document_storage(tmp_path, monkeypatch) -> str:
    """A storage root of this test's own, and the app pointed at it.

    Every test gets a fresh directory rather than a shared one: the layout is
    content-addressed, so two tests that uploaded the same bytes would otherwise see
    each other's file — and "the original is still there" would be a claim about
    somebody else's upload.

    The ceiling is left at the configured 50 MB except where a test lowers it: the
    byte counts are the interesting part, and a suite that patched the limit globally
    would never exercise the real number.
    """
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "document_storage_path", str(tmp_path))
    return str(tmp_path)


# --- helpers ----------------------------------------------------------------


async def post_upload(
    actor: Actor,
    content: bytes,
    *,
    filename: str = "politica.pdf",
    title: str = "Politica de vacaciones",
    **fields: object,
):
    """One upload, through the endpoint, as `actor`."""
    return await actor.post(
        "/api/v1/documents",
        files={"file": (filename, content, "application/octet-stream")},
        data={"title": title, **fields},
    )


async def ready(
    platform: Platform,
    actor: Actor,
    content: bytes,
    *,
    filename: str = "politica.txt",
    title: str = "Politica",
    **fields: object,
) -> dict:
    """Upload and parse in one step, and answer with the document as the API reads it.

    The parse is driven the way the job drives it — `DocumentService.parse_document`
    on its own session, committed — so a test that is about what a *ready* document
    looks like does not have to run the command.
    """
    response = await post_upload(actor, content, filename=filename, title=title, **fields)
    assert response.status_code == 201, response.text
    document_id = response.json()["id"]
    await run_parse(platform, document_id)
    read = await actor.get(f"/api/v1/documents/{document_id}")
    assert read.status_code == 200, read.text
    return read.json()


async def run_parse(platform: Platform, document_id: str, *, embedder: object = None) -> bool:
    """Parse one document exactly as the job does: system context, own session, one commit.

    `system_session` rather than a plain one, because the row-level policy on
    `documents` admits the pipeline only once it has published `app.current_system`.
    A test that skipped the flag would exercise a job that sees nothing — which is
    exactly the state this pipeline was in until the flag existed, and it failed
    silently.

    `embedder` defaults to the *configured* one, which in this container is the
    deterministic fake. A test that needs to see what the pipeline asked the embedding
    seam for — or that needs the call to fail — passes its own.
    """
    from app.config import get_settings
    from app.domain.document.embeddings import build_embedder
    from app.domain.document.storage import LocalFileStore
    from app.jobs.parse_documents import system_session

    settings = get_settings()
    async with system_session() as session:
        document = await PostgresDocumentRepository(session).get(UUID(document_id))
        if document is None:
            return False
        service = DocumentService(
            PostgresDocumentRepository(session),
            session,
            principal=None,  # type: ignore[arg-type]
            storage=LocalFileStore(settings.document_storage_path),
            embedder=(
                embedder
                if embedder is not None
                else build_embedder(
                    settings.embeddings_provider,
                    api_key=settings.openai_api_key,
                    base_url=settings.openai_base_url,
                )
            ),
        )
        await service.parse_document(UUID(document_id))
        await session.commit()
    return True


async def actions_for(platform: Platform, document_id: str) -> list[str]:
    rows = await platform.sql(
        "SELECT action FROM audit_log WHERE entity_id = :id ORDER BY id", {"id": document_id}
    )
    return [row[0] for row in rows]


async def chunks_of(platform: Platform, document_id: str) -> list[tuple]:
    return await platform.sql(
        "SELECT chunk_index, content, token_count FROM document_chunks "
        "WHERE document_id = :id ORDER BY chunk_index",
        {"id": document_id},
    )


# --- part 1: what may be uploaded -------------------------------------------


def test_the_accepted_formats_are_the_five_the_ticket_names() -> None:
    """PDF, DOCX, XLSX, TXT, Markdown — and the extension is what decides.

    A unit test rather than an HTTP one, because what it pins is the *table*: adding a
    sixth format is a deliberate edit here rather than something that happens.
    """
    from app.domain.document.files import SUPPORTED, accept

    assert set(SUPPORTED) == {".pdf", ".docx", ".xlsx", ".txt", ".md", ".markdown"}
    # The client's content type is never consulted: a `.pdf` is a PDF whatever the
    # multipart part claims, which is what stops `payroll.exe` reaching the PDF reader.
    # And the extension is matched case-insensitively, because a client that uppercases
    # a name is not sending a different format.
    assert accept("informe.PDF").media_type == "application/pdf"
    assert accept("informe.Pdf").extension == ".pdf"
    assert accept("notas.MD").media_type == "text/markdown"
    assert accept("payroll.exe") is None


@pytest.mark.parametrize(
    "filename",
    ["hoja.xls", "informe.doc", "script.sh", "photo.png", "archive.zip", "noextension"],
)
def test_an_unsupported_type_is_refused_with_a_readable_bilingual_message(
    filename: str,
) -> None:
    """The ticket's first line: other types are refused, with a readable prompt.

    "Readable" is asserted the way the product makes it true: the API answers with a
    catalogue *key*, both language catalogues hold a sentence for it, and the sentence
    names what may be uploaded instead. A refusal that only said "invalid" would pass a
    status-code assertion and fail the requirement.
    """
    from app.core.messages import MESSAGES

    assert ErrorCode.DOCUMENT_UPLOAD_TYPE_UNSUPPORTED.value == "ERR_DOC_002"
    key = "errors.document_upload_type_unsupported"
    for locale, catalogue in MESSAGES.items():
        sentence = catalogue[key]
        assert sentence.strip()
        if locale == "es":
            assert "PDF" in sentence and "Word" in sentence
        else:
            assert "PDF" in sentence and "Word" in sentence
    # And the extension the caller used is not in the table, which is the actual
    # decision — `filename` here is only the fixture.
    from app.domain.document.files import accept

    assert accept(filename) is None


async def test_uploading_an_unsupported_type_is_a_catalogued_422(
    platform: Platform, cast: Cast
) -> None:
    response = await post_upload(cast.uploader, b"#!/bin/sh\nrm -rf /", filename="deploy.sh")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == ErrorCode.DOCUMENT_UPLOAD_TYPE_UNSUPPORTED.value
    assert await platform.scalar("SELECT count(*) FROM documents") == 0


async def test_a_file_over_the_ceiling_is_refused_not_truncated(
    platform: Platform, cast: Cast, monkeypatch
) -> None:
    """The ceiling, refused — and refused *early*, which is the part that is testable.

    The real limit is 50 MB and the setting is what holds it, so this lowers it rather
    than uploading 50 MB: what the ticket requires is that a file over the limit is
    rejected rather than truncated, and that property does not depend on which number
    the limit is. `MAX_UPLOAD_BYTES` itself is asserted beside it, so a limit that
    quietly changed would fail here.
    """
    from app.config import get_settings

    assert MAX_UPLOAD_BYTES == 50 * 1024 * 1024
    assert get_settings().document_max_upload_bytes == MAX_UPLOAD_BYTES

    monkeypatch.setattr(get_settings(), "document_max_upload_bytes", 1024)
    response = await post_upload(cast.uploader, b"x" * 2048, filename="grande.txt")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == ErrorCode.DOCUMENT_UPLOAD_TOO_LARGE.value
    # Nothing was stored and nothing was recorded: a refusal that left half a file
    # behind would be the truncation the ticket forbids, wearing a 422.
    assert await platform.scalar("SELECT count(*) FROM documents") == 0


async def test_an_empty_upload_is_refused(
    platform: Platform, cast: Cast
) -> None:
    response = await post_upload(cast.uploader, b"", filename="vacio.txt")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == ErrorCode.DOCUMENT_UPLOAD_EMPTY.value


def test_a_crafted_filename_cannot_escape_the_storage_directory() -> None:
    """`../` in a name, normalised away before anything else sees it.

    Two claims, and the second is the one that matters: the *name* is reduced to one
    harmless segment, and the *stored path* does not consult the name at all — it is
    the content's digest. So traversal is impossible twice over, and the test asserts
    both rather than only the one that happens to be load-bearing today.
    """
    from app.domain.document.files import accept, safe_filename
    from app.domain.document.storage import storage_key

    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("..\\..\\Windows\\evil.pdf") == "evil.pdf"
    assert safe_filename("/etc/shadow.txt") == "shadow.txt"
    assert safe_filename("..") == FALLBACK_NAME
    assert safe_filename("....//....//x.pdf") == "x.pdf"
    # Control characters go, because the name travels into a header and a log line.
    assert safe_filename("in\r\nforme.pdf") == "informe.pdf"
    assert safe_filename(None) == FALLBACK_NAME
    # A name with no extension at all is not "unsafe": it is simply not a format the
    # table names, which is the type refusal and not this one.
    assert accept("../../etc/passwd") is None

    # The path: two different crafted names for the same bytes are one file, and the
    # key contains neither name.
    content = text_bytes()
    first = storage_key(content, ".txt")
    assert ".." not in first and "/etc" not in first
    assert first == storage_key(content, ".txt")
    assert first.endswith(".txt") and first.count("/") == 1
    assert accept("../../etc/passwd.txt").filename == "passwd.txt"


async def test_a_traversal_filename_is_stored_as_one_harmless_segment(
    platform: Platform, cast: Cast, document_storage: str
) -> None:
    """The same claim, end to end: the row's filename and the file on disk.

    The stored name is what a download is named and what a catalogue shows, so it is
    the half of the traversal defence a reader can actually see. The file itself lands
    under the root, one level down, named by its digest.
    """
    from pathlib import Path

    document = await ready(
        platform,
        cast.uploader,
        text_bytes(),
        filename="../../../etc/passwd.txt",
        title="Pasaporte",
    )

    assert document["filename"] == "passwd.txt"
    rows = await platform.sql(
        "SELECT storage_path FROM documents WHERE id = :id", {"id": document["id"]}
    )
    key = rows[0][0]
    assert key.startswith(document["content_sha256"][:2] + "/")
    stored = Path(document_storage) / key
    assert stored.is_file()
    # Nothing was written outside the root: the parent of the fan-out directory is the
    # root itself, and no `passwd` exists beside it.
    assert stored.resolve().is_relative_to(Path(document_storage).resolve())
    assert not (Path(document_storage).parent / "passwd.txt").exists()


# --- part 2: upload answers immediately -------------------------------------


async def test_upload_answers_before_parsing_and_leaves_the_document_processing(
    platform: Platform, cast: Cast
) -> None:
    """**The ticket's third line**: the HTTP request does not wait for the parse.

    Asserted as a fact about the response rather than about a duration — a timing
    assertion would be a flaky test of the machine. The response says `processing`, the
    row says `processing`, no chunk exists yet, and *then* the job takes it to `ready`.
    A pipeline that parsed inside the request would answer `ready` here and fail.

    The document is a PDF, so the parse is real work rather than a string read: if any
    step of it ever moved into the request, this is the test that notices.
    """
    response = await post_upload(
        cast.uploader,
        pdf_bytes("Politica de vacaciones", "Segunda pagina"),
        filename="politica.pdf",
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == DocumentStatus.PROCESSING.value
    assert body["stage"] == 1 and body["total_stages"] == 2
    assert body["chunk_count"] == 0 and body["extracted_chars"] == 0
    assert body["failure_reason"] is None

    rows = await platform.sql(
        "SELECT status FROM documents WHERE id = :id", {"id": body["id"]}
    )
    assert rows[0][0] == DocumentStatus.PROCESSING.value
    assert await chunks_of(platform, body["id"]) == []

    # And the background half, driven the way the command drives it.
    assert await parse_one(UUID(body["id"])) is not None
    after = await cast.uploader.get(f"/api/v1/documents/{body['id']}")
    assert after.json()["status"] == DocumentStatus.READY.value
    assert after.json()["stage"] == 2
    assert after.json()["page_count"] == 2


async def test_the_job_takes_a_processing_document_to_ready(platform: Platform, cast: Cast) -> None:
    """`parse_pending`, the command's own entry point, over what the table holds.

    The pass reports two worklists since ticket 32, and the second one — the documents
    whose vectors are missing — is empty here: the embedding happens inside the parse,
    in the same transaction that writes the chunks, so there is nothing to repair.
    """
    response = await post_upload(cast.uploader, text_bytes(), filename="politica.txt")
    assert response.status_code == 201, response.text

    counted = await parse_pending()

    assert counted == {
        "parsed": 1,
        "failed": 0,
        "missing": 0,
        "embedded": 0,
        "unembedded": 0,
    }
    document = (await cast.uploader.get("/api/v1/documents")).json()["items"][0]
    assert document["status"] == DocumentStatus.READY.value
    assert document["chunk_count"] >= 1
    # A second pass has nothing to do: the status left `processing` in the same
    # transaction that wrote the chunks, and the chunks were embedded with them.
    assert await parse_pending() == {
        "parsed": 0,
        "failed": 0,
        "missing": 0,
        "embedded": 0,
        "unembedded": 0,
    }


async def test_a_worker_that_parses_a_ready_document_rewrites_the_same_rows(
    platform: Platform, cast: Cast
) -> None:
    """Safe to run twice, which is what "just run it again" has to mean.

    The command accepts an explicit id so an operator can re-run one document; this is
    that path, twice, and the assertions are about the *result* rather than about a
    count of attempts: the same chunk rows, with the same indices.
    """
    document = await ready(platform, cast.uploader, text_bytes(), title="Politica")
    first = await chunks_of(platform, document["id"])

    assert await parse_one(UUID(document["id"])) is not None
    second = await chunks_of(platform, document["id"])

    assert first == second
    assert [row[0] for row in second] == list(range(len(second)))


# --- part 3: parsing, per format --------------------------------------------


@pytest.mark.parametrize(
    ("filename", "content", "expected"),
    [
        (
            "politica.txt",
            text_bytes("Veintitres dias laborables."),
            "Veintitres dias laborables.",
        ),
        (
            "politica.md",
            markdown_bytes("# Vacaciones\n\n## 1. Ambito\n\nTodo el personal."),
            "Todo el personal.",
        ),
        ("politica.pdf", pdf_bytes("Una pagina de texto"), "Una pagina de texto"),
        (
            "politica.docx",
            docx_bytes(("Vacaciones y permisos", "Treinta dias naturales.")),
            "Treinta dias naturales.",
        ),
        (
            "politica.xlsx",
            xlsx_bytes({"Vacaciones": (("Concepto", "Dias"), ("Anual", 23))}),
            "Anual",
        ),
        (
            "politica.markdown",
            markdown_bytes("## Permisos\n\nMatrimonio: quince dias."),
            "Matrimonio: quince dias.",
        ),
    ],
)
async def test_each_format_is_parsed_by_its_own_reader(
    platform: Platform, cast: Cast, filename: str, content: bytes, expected: str
) -> None:
    """One test per accepted extension, through the endpoint and the job.

    The expected fragment is text only that format's reader can produce — a PDF's `Tj`
    string, a docx paragraph, a spreadsheet cell — so a reader wired to the wrong media
    type fails here rather than silently producing an empty document and a `failed`
    status that reads like a bad fixture.
    """
    document = await ready(platform, cast.uploader, content, filename=filename, title=filename)

    assert document["status"] == DocumentStatus.READY.value, document["failure_reason"]
    assert document["extracted_chars"] > 0
    stored = await chunks_of(platform, document["id"])
    assert stored, "a ready document with no chunks is the silent empty document"
    assert expected in " ".join(row[1] for row in stored)


async def test_a_docx_table_is_content_too(platform: Platform, cast: Cast) -> None:
    """A table cell's text is what the document says, and dropping it loses content.

    A policy matrix is the ordinary case: the answer is in the table and nowhere in the
    prose.
    """
    document = await ready(
        platform,
        cast.uploader,
        docx_bytes(("Anexo I",), (("Antiguedad", "Dias"), ("Tres anos", 25))),
        filename="anexo.docx",
        title="Anexo I",
    )

    stored = " ".join(row[1] for row in await chunks_of(platform, document["id"]))
    assert "Tres anos" in stored and "25" in stored


async def test_an_xlsx_sheet_name_travels_as_a_heading(platform: Platform, cast: Cast) -> None:
    """So a chunk can say which sheet it came from, the way a PDF's says which page."""
    document = await ready(
        platform,
        cast.uploader,
        xlsx_bytes({"Vacaciones": (("Anual", 23),), "Permisos": (("Matrimonio", 15),)}),
        filename="cuadro.xlsx",
        title="Cuadro",
    )

    rows = await platform.sql(
        "SELECT heading_path, content FROM document_chunks WHERE document_id = :id "
        "ORDER BY chunk_index",
        {"id": document["id"]},
    )
    headings = {row[0] for row in rows}
    assert "# Vacaciones" in headings
    assert any("Matrimonio" in row[1] for row in rows)


# --- part 4: no text means failed, and there is no OCR ----------------------


async def test_a_scanned_pdf_fails_with_the_message_the_ticket_words(
    platform: Platform, cast: Cast
) -> None:
    """**The ticket's sixth line**, and the case that is easiest to get quietly wrong.

    A PDF with pages and no extractable text is a scan. It must go to `failed` — never
    to `ready` with an empty document — with `no text extracted; upload a text version`
    as its reason, and it must keep *no chunks*: a ready-but-empty document would
    answer retrieval with nothing while claiming to be indexed, which is the silent
    failure the ticket forbids by name. There is no OCR in this pipeline, so nothing
    here tries again.
    """
    response = await post_upload(cast.uploader, scanned_pdf_bytes(2), filename="escaneado.pdf")
    assert response.status_code == 201, response.text
    document_id = response.json()["id"]

    assert await parse_one(UUID(document_id)) is not None
    document = (await cast.uploader.get(f"/api/v1/documents/{document_id}")).json()

    assert document["status"] == DocumentStatus.FAILED.value
    assert document["failure_reason"] == NO_TEXT_MESSAGE
    assert document["failure_reason"] == "no text extracted; upload a text version"
    assert document["extracted_chars"] == 0
    assert document["chunk_count"] == 0
    assert await chunks_of(platform, document_id) == []


async def test_a_workbook_with_no_values_fails_the_same_way(
    platform: Platform, cast: Cast
) -> None:
    """The refusal is about text, not about PDFs: any format that says nothing fails."""
    document = await ready(
        platform,
        cast.uploader,
        empty_xlsx_bytes(),
        filename="vacio.xlsx",
        title="Vacio",
    )

    assert document["status"] == DocumentStatus.FAILED.value
    assert document["failure_reason"] == NO_TEXT_MESSAGE


async def test_a_failed_document_can_still_be_opened_as_a_file(
    platform: Platform, cast: Cast
) -> None:
    """The original is still there — it is a *parse* that failed, not a storage one.

    Stated as its own test because the two are easy to conflate: `failed` says the
    pipeline found no text, and the file the uploader sent is exactly what somebody
    needs in order to see that they sent a scan.
    """
    content = scanned_pdf_bytes()
    response = await post_upload(cast.uploader, content, filename="escaneado.pdf")
    document_id = response.json()["id"]
    await parse_one(UUID(document_id))

    download = await cast.uploader.get(f"/api/v1/documents/{document_id}/content")

    assert download.status_code == 200
    assert download.content == content
    assert download.headers["content-type"] == "application/pdf"
    assert download.headers["x-document-sha256"]


# --- part 5: the record -----------------------------------------------------


async def test_the_document_carries_the_fields_the_ticket_lists(
    platform: Platform, cast: Cast
) -> None:
    """Title, department, clearance, owner, category, tags, language, status, path,
    hash, text length and `is_company_kb` — one assertion per field, from the row.

    Read from PostgreSQL rather than from the response body, because half of these are
    columns the ticket names and the API's projection is a separate claim (asserted
    below, from the response).
    """
    from pathlib import Path

    from app.config import get_settings

    response = await post_upload(
        cast.uploader,
        text_bytes(),
        filename="politica.txt",
        title="Politica de vacaciones",
        department_id=cast.department,
        clearance_level="low",
        category="rrhh",
        tags="vacaciones, 2026, vacaciones",
        language="es",
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["id"]
    await parse_one(UUID(document_id))

    rows = await platform.sql(
        """
        SELECT title, department_id, clearance_level, owner_employee_id, category,
               tags, language, status, storage_path, content_sha256, extracted_chars,
               is_company_kb, visibility, filename, media_type, file_size, chunk_count
        FROM documents WHERE id = :id
        """,
        {"id": document_id},
    )
    row = dict(
        zip(
            (
                "title",
                "department_id",
                "clearance_level",
                "owner",
                "category",
                "tags",
                "language",
                "status",
                "path",
                "sha",
                "chars",
                "company",
                "visibility",
                "filename",
                "media_type",
                "size",
                "chunks",
            ),
            rows[0],
            strict=True,
        )
    )

    assert row["title"] == "Politica de vacaciones"
    # The ids cross the wire as strings and come back from `platform.sql` as `UUID`s,
    # so the comparison is on the string form — which is also the form the API returns.
    assert str(row["department_id"]) == cast.department
    assert row["clearance_level"] == "low"
    assert str(row["owner"]) == cast.uploader.employee_id
    assert row["category"] == "rrhh"
    # JSONB, deduplicated: `tags` is a list of labels, so "everything tagged
    # vacaciones" is a containment query rather than a substring one.
    assert row["tags"] == ["vacaciones", "2026"]
    assert row["language"] == "es"
    assert row["status"] == DocumentStatus.READY.value
    assert row["path"].endswith(".txt") and row["path"].startswith(row["sha"][:2])
    assert len(row["sha"]) == 64
    assert row["chars"] > 0
    assert row["company"] is False
    assert row["visibility"] == "private"
    assert row["filename"] == "politica.txt"
    assert row["media_type"] == "text/plain"
    assert row["size"] == len(text_bytes())
    assert row["chunks"] >= 1

    # The bytes really are on disk at the key the row names, under the configured root.
    assert (Path(get_settings().document_storage_path) / row["path"]).read_bytes() == text_bytes()


async def test_a_company_document_has_no_owner_and_needs_a_department(
    platform: Platform, cast: Cast
) -> None:
    """The design's own annotation, as a fact about the row.

    A company document is nobody's: it is reached through its department, which is why
    one without a department cannot be stored — `test_the_database_refuses_a_company_
    document_without_a_department` below asserts that half.
    """
    response = await post_upload(
        cast.admin,
        text_bytes("Normativa interna."),
        filename="normativa.txt",
        title="Normativa interna",
        department_id=cast.department,
        is_company_kb=True,
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["owner_employee_id"] is None
    assert body["is_company_kb"] is True
    assert body["visibility"] == "company"
    assert body["uploaded_by_employee_id"] == cast.admin.employee_id


async def test_an_ordinary_employee_cannot_create_a_company_document(
    platform: Platform, cast: Cast
) -> None:
    """`is_company_kb` moves a document from §4.2's ownership clause to its department
    clause — it stops being somebody's file and becomes the organisation's. That is
    `document.manage`, and an employee does not hold it."""
    response = await post_upload(
        cast.uploader,
        text_bytes(),
        filename="normativa.txt",
        title="Normativa",
        department_id=cast.department,
        is_company_kb=True,
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == ErrorCode.FORBIDDEN.value
    assert await platform.scalar("SELECT count(*) FROM documents") == 0


async def test_a_company_document_without_a_department_is_refused(
    platform: Platform, cast: Cast
) -> None:
    response = await post_upload(
        cast.admin,
        text_bytes(),
        filename="normativa.txt",
        title="Normativa",
        is_company_kb=True,
    )

    assert response.status_code == 400, response.text
    assert await platform.scalar("SELECT count(*) FROM documents") == 0


async def test_a_document_cannot_be_filed_into_a_department_the_caller_cannot_reach(
    platform: Platform, cast: Cast
) -> None:
    """A document must not be filed where its own author could never read it."""
    response = await post_upload(
        cast.uploader,
        text_bytes(),
        filename="politica.txt",
        title="Politica",
        department_id=cast.other_department,
    )

    assert response.status_code == 403, response.text


async def test_a_clearance_above_the_callers_own_is_refused(
    platform: Platform, cast: Cast
) -> None:
    """§4.2's ceiling is the kernel's, and it applies to the act of filing too.

    An upload classified above its author's own level would be a document its author
    cannot open — accepted, then hidden from the person who just created it.
    """
    response = await post_upload(
        cast.uploader,
        text_bytes(),
        filename="politica.txt",
        title="Politica",
        clearance_level="high",
    )

    assert response.status_code == 403, response.text
    assert await platform.scalar("SELECT count(*) FROM documents") == 0


# --- part 6: retrieval, deduplication and audit -----------------------------


async def test_the_original_is_kept_and_downloadable_under_the_read_rule(
    platform: Platform, cast: Cast
) -> None:
    """**The ticket's seventh line**: the original is kept, and reachable behind a
    permission-guarded endpoint that uses the *read's* rule.

    Three claims in one test, because they are one requirement: the bytes come back
    identical to what was uploaded; the caller who uploaded it can read them; and a
    caller who cannot read the *document* cannot read the *file* either — which is what
    "the same rule, not a second one" means. The outsider is in another department and
    the document is a personal upload with no department at all, so §4.2's second clause
    fails for them and its first does too.
    """
    content = text_bytes("Veintitres dias laborables.")
    document = await ready(platform, cast.uploader, content, title="Politica")

    mine = await cast.uploader.get(f"/api/v1/documents/{document['id']}/content")
    assert mine.status_code == 200
    assert mine.content == content
    assert mine.headers["content-disposition"].startswith("attachment;")
    assert mine.headers["content-length"] == str(len(content))

    theirs = await cast.outsider.get(f"/api/v1/documents/{document['id']}/content")
    assert theirs.status_code == 404
    assert theirs.json()["error"]["code"] == ErrorCode.DOCUMENT_NOT_FOUND.value

    # And the metadata endpoint refuses the same caller the same way: one rule, so the
    # two endpoints cannot disagree about who may see this document.
    assert (await cast.outsider.get(f"/api/v1/documents/{document['id']}")).status_code == 404


async def test_a_colleague_in_the_same_department_cannot_read_a_personal_upload(
    platform: Platform, cast: Cast
) -> None:
    """§4.2's first clause is ownership, and sharing a department is not ownership.

    The clause the generic department-and-clearance path would have allowed: the
    document has no department and no classification above the caller's, so a rule
    written as "department and clearance" would have said yes.
    """
    document = await ready(platform, cast.uploader, text_bytes(), title="Politica")

    assert (await cast.colleague.get(f"/api/v1/documents/{document['id']}")).status_code == 404
    assert (
        await cast.colleague.get(f"/api/v1/documents/{document['id']}/content")
    ).status_code == 404


async def test_a_company_document_is_readable_inside_its_department(
    platform: Platform, cast: Cast
) -> None:
    """And the second clause is what makes a company document a company document."""
    response = await post_upload(
        cast.admin,
        text_bytes("Normativa interna."),
        filename="normativa.txt",
        title="Normativa interna",
        department_id=cast.department,
        is_company_kb=True,
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["id"]

    # The uploader's own department: the colleague reads it.
    assert (await cast.colleague.get(f"/api/v1/documents/{document_id}")).status_code == 200
    # Another department, and the colleague's clearance is not in question: clause 2
    # fails, clause 1 fails (no owner), and the exception roles are not this caller.
    assert (await cast.outsider.get(f"/api/v1/documents/{document_id}")).status_code == 404


async def test_the_list_shows_only_what_the_caller_may_read(
    platform: Platform, cast: Cast
) -> None:
    """The list is the kernel's answer too, not a second query with its own rules.

    Three documents, three visibility outcomes, and the outsider sees exactly one — the
    company document of *their* department. A list endpoint that forgot its filter is
    the defect `filter_for` exists to make unwritable, and this is where that claim is
    checked from outside.
    """
    mine = await ready(platform, cast.uploader, text_bytes("Mio."), title="Mio", filename="mio.txt")
    response = await post_upload(
        cast.admin,
        text_bytes("Normativa interna."),
        filename="normativa.txt",
        title="Normativa interna",
        department_id=cast.department,
        is_company_kb=True,
    )
    company = response.json()["id"]
    other = await ready(
        platform,
        cast.outsider,
        text_bytes("Ajeno."),
        title="Ajeno",
        filename="ajeno.txt",
        department_id=cast.other_department,
    )

    listed = (await cast.outsider.get("/api/v1/documents")).json()["items"]
    ids = {item["id"] for item in listed}
    assert other["id"] in ids
    assert mine["id"] not in ids
    assert company not in ids

    mine_listed = (await cast.uploader.get("/api/v1/documents")).json()["items"]
    mine_ids = {item["id"] for item in mine_listed}
    assert mine["id"] in mine_ids
    assert company in mine_ids
    assert other["id"] not in mine_ids


async def test_uploading_the_same_file_twice_is_a_conflict_naming_the_first(
    platform: Platform, cast: Cast
) -> None:
    """**The ticket's eighth line**: the content hash recognises the file.

    A 409 rather than an idempotent 200 — the router's docstring argues the choice —
    and the existing document's id travels in the detail so the client can offer the one
    useful action. The file on disk is one object, and the metadata of the second
    request changed nothing.
    """
    content = text_bytes("El mismo contenido.")
    first = await ready(platform, cast.uploader, content, title="Primera")

    second = await post_upload(cast.uploader, content, filename="copia.txt", title="Segunda")
    assert second.status_code == 409, second.text
    error = second.json()["error"]
    assert error["code"] == ErrorCode.DOCUMENT_DUPLICATE.value
    # The id is what makes the refusal actionable rather than merely correct.
    assert first["id"] in (error["detail"] or "")

    assert await platform.scalar("SELECT count(*) FROM documents") == 1
    assert await platform.scalar("SELECT count(DISTINCT storage_path) FROM documents") == 1

    # The same bytes, the same *caller*: refused. A different caller sees no conflict,
    # because the duplicate rule is scoped to what they can read — a global one would
    # answer the second uploader with a document they may not open.
    theirs = await ready(platform, cast.colleague, content, title="Suya")
    assert theirs["id"] != first["id"]
    assert await platform.scalar("SELECT count(*) FROM documents") == 2


async def test_upload_parse_success_and_parse_failure_are_all_audited(
    platform: Platform, cast: Cast
) -> None:
    """**The ticket's ninth line**: three events, three rows.

    Read from `audit_log` keyed on the document's own id, so "everything that happened
    to this document" is one equality filter. The upload is attributed to the person who
    made the request; the parse is the system's, which is what `initiated_by` is for —
    attributing a nightly pass to whoever happened to upload the file would be a lie the
    trail tells about itself.
    """
    good = await ready(platform, cast.uploader, text_bytes(), title="Buena")
    assert await actions_for(platform, good["id"]) == [
        AuditAction.DOCUMENT_UPLOADED.value,
        AuditAction.DOCUMENT_PARSED.value,
    ]

    response = await post_upload(cast.uploader, scanned_pdf_bytes(), filename="escaneado.pdf")
    failed_id = response.json()["id"]
    await parse_one(UUID(failed_id))

    assert await actions_for(platform, failed_id) == [
        AuditAction.DOCUMENT_UPLOADED.value,
        AuditAction.DOCUMENT_PARSE_FAILED.value,
    ]
    reason = await platform.scalar(
        "SELECT after ->> 'failure_reason' FROM audit_log WHERE entity_id = :id "
        "AND action = 'document.parse_failed'",
        {"id": failed_id},
    )
    assert reason == NO_TEXT_MESSAGE

    actors = await platform.sql(
        "SELECT action, actor_user_id, initiated_by FROM audit_log WHERE entity_id = :id "
        "ORDER BY id",
        {"id": good["id"]},
    )
    assert str(actors[0][1]) == cast.uploader.user_id
    assert actors[1][1] is None and actors[1][2] == "system"


# --- part 7: retry ----------------------------------------------------------


async def test_reprocessing_produces_no_duplicate_chunks(
    platform: Platform, cast: Cast
) -> None:
    """**The ticket's tenth line**, and the invariant the mutation test breaks.

    A ready document is re-processed: the chunks are cleared by `reprocess` in its own
    transaction, the job re-parses, and the result is the same rows at the same indices
    — never the first set plus a second. The total count is the assertion that catches
    an append, and the index list is what catches an append that happened to renumber.
    """
    # Five sections of real prose, long enough that the split has to make more than one
    # chunk: a single-chunk fixture would make "no duplicate chunks" true by arithmetic
    # rather than by the delete-then-insert the test is about.
    paragraph = (
        "El personal con al menos un ano de antiguedad podra solicitar dias de "
        "vacaciones adicionales, que se concederan por orden de peticion y siempre "
        "que el servicio quede cubierto. "
    )
    body = "# Vacaciones\n\n" + "\n\n".join(
        f"## {index}. Seccion\n\n{paragraph * 3}" for index in range(1, 6)
    )
    document = await ready(
        platform, cast.uploader, markdown_bytes(body), filename="manual.md", title="Manual"
    )
    before = await chunks_of(platform, document["id"])
    assert len(before) >= 2, "the fixture must produce several chunks for this to mean anything"

    reprocessed = await cast.uploader.post(f"/api/v1/documents/{document['id']}/reprocess")
    assert reprocessed.status_code == 200, reprocessed.text
    assert reprocessed.json()["status"] == DocumentStatus.PROCESSING.value
    # The removal is synchronous and the re-parse is not: between the two calls the
    # document has no chunks at all, which is what makes a retry impossible to
    # interleave into a doubled set.
    assert await chunks_of(platform, document["id"]) == []

    await parse_one(UUID(document["id"]))
    after = await chunks_of(platform, document["id"])

    assert after == before
    assert len(after) == len(before)


async def test_a_failed_parse_can_be_retried_and_succeeds_once_the_text_is_there(
    platform: Platform, cast: Cast, document_storage: str
) -> None:
    """A scan fails; replacing the stored original with a text version and retrying
    makes it ready.

    This is the *point* of `reprocess`: the ticket's "解析任务的失败可重试" is only true
    if a retry can produce a different answer, and a retry that re-read the same bytes
    and reached the same refusal would be a button that does nothing.
    """
    from pathlib import Path

    from app.config import get_settings

    response = await post_upload(cast.uploader, scanned_pdf_bytes(), filename="escaneado.pdf")
    document_id = response.json()["id"]
    await parse_one(UUID(document_id))
    assert (await cast.uploader.get(f"/api/v1/documents/{document_id}")).json()["status"] == (
        DocumentStatus.FAILED.value
    )

    # The uploader sends a text version. Same document: the row is replaced in place
    # with the new bytes, which is what a "the scan was wrong, here is the export"
    # correction looks like without a version table (see `models/document.py`).
    key = await platform.scalar(
        "SELECT storage_path FROM documents WHERE id = :id", {"id": document_id}
    )
    (Path(document_storage) / key).write_bytes(text_bytes("Ahora si hay texto."))
    digest = await platform.scalar(
        "SELECT content_sha256 FROM documents WHERE id = :id", {"id": document_id}
    )
    await platform.sql(
        "UPDATE documents SET media_type = 'text/plain' WHERE id = :id", {"id": document_id}
    )

    retried = await cast.uploader.post(f"/api/v1/documents/{document_id}/reprocess")
    assert retried.status_code == 200, retried.text
    await parse_one(UUID(document_id))

    after = (await cast.uploader.get(f"/api/v1/documents/{document_id}")).json()
    assert after["status"] == DocumentStatus.READY.value
    assert after["failure_reason"] is None
    assert after["extracted_chars"] > 0
    assert await chunks_of(platform, document_id)
    assert digest == await platform.scalar(
        "SELECT content_sha256 FROM documents WHERE id = :id", {"id": document_id}
    )
    assert get_settings().document_storage_path == document_storage


async def test_reprocessing_a_document_being_parsed_is_refused(
    platform: Platform, cast: Cast
) -> None:
    """Two parsers writing one row is a race nothing here needs."""
    response = await post_upload(cast.uploader, text_bytes(), filename="politica.txt")
    document_id = response.json()["id"]

    refused = await cast.uploader.post(f"/api/v1/documents/{document_id}/reprocess")

    assert refused.status_code == 409, refused.text
    assert (
        refused.json()["error"]["code"] == ErrorCode.DOCUMENT_REPROCESS_UNSUPPORTED.value
    )


async def test_a_retry_does_not_write_a_second_copy_of_the_original(
    platform: Platform, cast: Cast
) -> None:
    """Storage is content-addressed, so a re-parse is a read and never a write.

    Asserted with the recording double rather than against the volume, because the claim
    is about *operations*: a thousand retries are a thousand reads and no writes. The
    double starts empty on purpose, so this also shows the failure path is wired — a
    store that does not have the file fails the document rather than succeeding quietly.
    """
    document = await ready(
        platform, cast.uploader, text_bytes("Contenido estable."), title="Estable"
    )
    key = await platform.scalar(
        "SELECT storage_path FROM documents WHERE id = :id", {"id": document["id"]}
    )

    recorder = RecordingFileStore()
    async with platform.factory() as session:
        service = DocumentService(
            PostgresDocumentRepository(session),
            session,
            principal=None,  # type: ignore[arg-type]
            storage=recorder,
        )
        outcome = await service.parse_document(UUID(document["id"]))
        await session.commit()

    assert outcome.failed
    assert recorder.puts == []
    assert recorder.gets == [key]
    # And the real root still holds it: the retry that could not read it wrote nothing.
    rows = await platform.sql(
        "SELECT count(*) FROM document_chunks WHERE document_id = :id", {"id": document["id"]}
    )
    assert rows[0][0] == 0


async def test_a_missing_original_fails_the_document_rather_than_the_job(
    platform: Platform, cast: Cast
) -> None:
    """A row whose file has gone is a data error the operator sees, not a crash.

    The document goes to `failed` with a reason that says so — which is the difference
    between "this file is a scan" and "this file is missing", and the reason the
    failure is a value rather than an exception.
    """
    document = await ready(platform, cast.uploader, text_bytes(), title="Perdido")
    await platform.sql(
        "DELETE FROM document_chunks WHERE document_id = :id", {"id": document["id"]}
    )
    await platform.sql(
        "UPDATE documents SET storage_path = 'aa/' || content_sha256 || '.txt' WHERE id = :id",
        {"id": document["id"]},
    )

    assert await parse_one(UUID(document["id"])) is not None
    after = (await cast.uploader.get(f"/api/v1/documents/{document['id']}")).json()
    assert after["status"] == DocumentStatus.FAILED.value
    assert "missing" in after["failure_reason"]
    # And the download says which of the two it is, rather than 404-ing like a
    # permission refusal would.
    missing = await cast.uploader.get(f"/api/v1/documents/{document['id']}/content")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == ErrorCode.DOCUMENT_FILE_MISSING.value


# --- part 8: row-level security, over the restricted role -------------------


@pytest.fixture
async def app_connection(settings: Settings) -> AsyncIterator[async_sessionmaker]:
    """Sessions bound to the restricted role, on the test database.

    The same fixture `test_permission_matrix.py` builds, for the same reason: a table's
    owner is exempt from its own policies, so a suite connected as `eam` would exercise
    none of this and still look green.
    """
    restricted = create_async_engine(settings.runtime_test_database_url)
    try:
        yield async_sessionmaker(bind=restricted, expire_on_commit=False)
    finally:
        await restricted.dispose()


async def publish(session, employee_id: str, clearance_levels: list[str], departments: list[str]):
    """The context a request publishes, written out by hand.

    Deliberately not `kernel.apply_rls_context`: a test that called the kernel's own
    function would prove the two agree about a *name* and nothing about what PostgreSQL
    does with the value.
    """
    arrays = {
        "app.clearance_levels": "{" + ",".join(f'"{value}"' for value in clearance_levels) + "}",
        "app.department_ids": "{" + ",".join(f'"{value}"' for value in departments) + "}",
        "app.current_employee_id": employee_id,
    }
    for name, value in arrays.items():
        await session.execute(
            text("SELECT set_config(:name, :value, true)"), {"name": name, "value": value}
        )


async def test_the_parsing_loop_is_on_in_development_and_off_elsewhere() -> None:
    """The pipeline has two supported ways to run, and the settings say which is which.

    The **command** is the one production uses — `python -m app.jobs.parse_documents`
    from cron or the worker container — and the in-process loop exists because
    `docker compose up` has no scheduler at all, so without it an upload sits in
    `processing` for ever and the screen honestly says so. That reads as a broken
    pipeline rather than as an unconfigured one, which is why the default is derived
    from the environment instead of being something a deployment has to remember to
    turn on.

    **`APP_ENV` is `development` in this container**, which is worth asserting rather
    than assuming: the check below reads the real value and says what follows from it.
    The suite is unaffected either way — the test client drives the application through
    `ASGITransport`, which does not run the lifespan, so no loop is started while tests
    assert about rows.
    """
    from contextlib import suppress

    from app.config import Settings, get_settings

    settings = get_settings()
    assert settings.is_development, f"APP_ENV is {settings.app_env!r} in this container"
    assert settings.parses_documents_in_process is True

    # `None` is stated rather than inherited, and that is the point of this line:
    # `docker-compose.yml` exports `DOCUMENT_PARSE_RUNNER_ENABLED=true` so a
    # `docker compose up` stack parses without a scheduler, and a `Settings` built here
    # inherits it. This assertion is about the *derivation* — off outside development —
    # so inheriting would make it assert the ambient environment instead. It did, from
    # the moment the API container was rebuilt from that compose file. The two
    # assertions below are the explicit-pin half of the same rule.
    production = Settings(app_env="production", document_parse_runner_enabled=None)
    assert production.parses_documents_in_process is False

    # An installation that installs a real scheduler turns it off explicitly, and the
    # explicit value wins over the environment — in both directions.
    pinned = Settings(app_env="development", document_parse_runner_enabled=False)
    assert pinned.parses_documents_in_process is False
    forced = Settings(app_env="production", document_parse_runner_enabled=True)
    assert forced.parses_documents_in_process is True

    # And the loop itself is the same function the command runs, wrapped in a sleep:
    # starting it and cancelling it must not raise, which is what a broken loop would
    # do at import or at the first await.
    from app.jobs.parse_documents import run_forever

    task = asyncio.create_task(run_forever(3600))
    await asyncio.sleep(0)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_the_policies_are_attached_to_both_tables(settings: Settings) -> None:
    """Ticket 13's predicate, on the tables it was written for.

    Read from the catalogue rather than from the migration file, because the claim is
    about what PostgreSQL has: a policy that was written but never attached would leave
    the function present and the tables ungoverned.

    Four policies on `documents` and one on `document_chunks`, and the counts are
    literal so that removing one is a failing test. Every one of them was added because
    something failed silently without it, and each failure is worth naming here:

    * `documents_visibility` — the read rule, ticket 13's predicate.
    * `documents_insert` — without it *every upload* was refused, because a table with
      RLS and only a `FOR SELECT` policy applies that policy to a new row.
    * `documents_system` — without it the parsing job saw no documents at all and
      reported "0 parsed" while looking healthy.
    * `documents_write` — without it `reprocess` updated zero rows and answered 200 with
      the old status, because a permissive `ALL` policy whose `WITH CHECK` fails makes
      the whole UPDATE affect nothing.
    * `document_chunks_access` — a chunk is readable exactly when its document is, and
      writable only by the pipeline.
    """
    engine = create_async_engine(settings.test_database_url)
    try:
        async with engine.connect() as connection:
            policies = (
                await connection.execute(
                    text(
                        "SELECT tablename, policyname, cmd, qual, with_check FROM pg_policies "
                        "WHERE tablename IN ('documents', 'document_chunks') "
                        "ORDER BY tablename, policyname"
                    )
                )
            ).all()
            relrowsecurity = (
                await connection.execute(
                    text(
                        "SELECT relname, relrowsecurity FROM pg_class "
                        "WHERE relname IN ('documents', 'document_chunks') ORDER BY relname"
                    )
                )
            ).all()
    finally:
        await engine.dispose()

    assert {row.tablename for row in policies} == {"documents", "document_chunks"}
    assert all(row.relrowsecurity for row in relrowsecurity)
    assert len(policies) == 5
    assert {row.policyname for row in policies} == {
        "documents_visibility",
        "documents_insert",
        "documents_system",
        "documents_write",
        "document_chunks_access",
    }
    # The visibility rule is ticket 13's helper everywhere a caller can be admitted: the
    # read, the update, and the chunk through its document. A rule spelled out again
    # here is a rule that can drift from the kernel's.
    by_name = {row.policyname: row for row in policies}
    assert "document_visibility_predicate" in by_name["documents_visibility"].qual
    assert "document_visibility_predicate" in by_name["documents_write"].with_check
    assert "document_visibility_predicate" in by_name["document_chunks_access"].qual
    # The rules that cannot use the helper — `WITH CHECK` runs before the row exists —
    # state the same thing forwards: yours, in a department you are in, or the system's.
    assert "app.current_employee_id" in by_name["documents_insert"].with_check
    assert "app.current_system" in by_name["documents_system"].qual
    assert "app.current_system" in by_name["document_chunks_access"].with_check


async def test_the_runtime_role_can_use_the_tables_and_cannot_delete_a_document(
    settings: Settings,
) -> None:
    """The privileges the migration states, asserted rather than assumed.

    Migration 0007's `ALTER DEFAULT PRIVILEGES` is what makes a table added later
    reachable by the runtime role without another grant — the claim its comment makes.
    `DELETE` on `documents` is the one privilege deliberately withheld: the original
    file is kept, and a role that can delete the row can orphan the bytes.
    """
    engine = create_async_engine(settings.test_database_url)
    try:
        async with engine.connect() as connection:
            privileges = (
                await connection.execute(
                    text(
                        "SELECT has_table_privilege(:role, 'documents', 'SELECT'), "
                        "has_table_privilege(:role, 'documents', 'INSERT'), "
                        "has_table_privilege(:role, 'documents', 'UPDATE'), "
                        "has_table_privilege(:role, 'documents', 'DELETE'), "
                        "has_table_privilege(:role, 'document_chunks', 'DELETE')"
                    ),
                    {"role": APP_ROLE},
                )
            ).one()
    finally:
        await engine.dispose()

    assert tuple(privileges) == (True, True, True, False, True)


async def test_a_document_outside_the_callers_department_is_invisible_to_the_database(
    platform: Platform, cast: Cast, app_connection: async_sessionmaker
) -> None:
    """**The ticket's ninth checklist line, at the database layer.**

    A document in another department, classified above the caller's clearance: it is
    invisible under a published context, and it is invisible again when the caller's own
    department is the only one in the context. Then the *chunk* of a document the caller
    cannot read is invisible too — which is the half that matters most, because a
    retrieval query reads chunks and would otherwise quote from a file the reader may
    not open.

    The rows are written as the owner: a policy is what the restricted role is subject
    to, and the fixture is not.
    """
    # A company document in the other department at `high`, with a chunk: the two rows
    # the caller must not be able to reach.
    company = await platform.sql(
        """
        INSERT INTO documents (id, title, owner_employee_id, department_id, clearance_level,
                               visibility, is_company_kb, tags, language, status, storage_path,
                               content_sha256, filename, media_type, file_size, extracted_chars)
        VALUES (:id, 'Normativa de direccion', NULL, :department, 'high', 'company', true,
                '[]'::jsonb, 'es', 'ready', 'aa/deadbeef.txt', repeat('a', 64), 'n.txt',
                'text/plain', 10, 20)
        RETURNING id
        """,
        {"id": uuid4(), "department": cast.other_department},
    )
    company_id = str(company[0][0])
    await platform.sql(
        """
        INSERT INTO document_chunks (id, document_id, chunk_index, content, token_count,
                                     chunking_version)
        VALUES (:id, :document_id, 0, 'Contenido reservado.', 5, 'structural-v1')
        """,
        {"id": uuid4(), "document_id": company_id},
    )

    # And one in the caller's own department, at a level they hold: the control, so a
    # policy that returned nothing at all would fail here rather than pass.
    visible = await platform.sql(
        """
        INSERT INTO documents (id, title, owner_employee_id, department_id, clearance_level,
                               visibility, is_company_kb, tags, language, status, storage_path,
                               content_sha256, filename, media_type, file_size, extracted_chars)
        VALUES (:id, 'Normativa del departamento', NULL, :department, 'low', 'company', true,
                '[]'::jsonb, 'es', 'ready', 'bb/cafebabe.txt', repeat('b', 64), 'v.txt',
                'text/plain', 10, 12)
        RETURNING id
        """,
        {"id": uuid4(), "department": cast.department},
    )
    visible_id = str(visible[0][0])
    await platform.sql(
        """
        INSERT INTO document_chunks (id, document_id, chunk_index, content, token_count,
                                     chunking_version)
        VALUES (:id, :document_id, 0, 'Contenido disponible.', 5, 'structural-v1')
        """,
        {"id": uuid4(), "document_id": visible_id},
    )

    async with app_connection() as session:
        # The context a request publishes: this employee, low clearance, their own
        # department and nothing else.
        await publish(
            session,
            cast.uploader.employee_id,
            ["low"],
            [cast.department],
        )
        documents = set(
            (
                await session.execute(text("SELECT id::text FROM documents"))
            ).scalars()
        )
        chunks = set(
            (
                await session.execute(text("SELECT document_id::text FROM document_chunks"))
            ).scalars()
        )

    assert visible_id in documents
    assert company_id not in documents, (
        "a document outside the caller's department and above their clearance was "
        "visible to the restricted role"
    )
    assert chunks == {visible_id}
    assert company_id not in chunks, (
        "the chunk of a document the caller cannot read was readable"
    )


async def test_the_system_flag_is_the_pipelines_alone(
    platform: Platform, cast: Cast, app_connection: async_sessionmaker
) -> None:
    """The parsing job's reach, and the proof it is not a request's.

    Two halves, and the second is the one that matters: without the flag the runtime
    role sees none of the documents it would have to parse — which is why the job
    publishes it — and *with* it the job still cannot be reached from a request,
    because `apply_rls_context` never writes the setting and this fixture has to write
    it by hand to demonstrate the difference.
    """
    document = await ready(platform, cast.uploader, text_bytes("Contenido."), title="Pipeline")

    async with app_connection() as session:
        # A request's context: somebody else entirely, so the visibility rule cannot be
        # what admits the row.
        await publish(session, str(uuid4()), ["low"], [])
        hidden = await session.scalar(text("SELECT count(*) FROM documents"))

    async with app_connection() as session:
        await session.execute(
            text("SELECT set_config('app.current_system', 'true', true)")
        )
        seen = (
            (await session.execute(text("SELECT id::text FROM documents"))).scalars().all()
        )
        chunks = await session.scalar(text("SELECT count(*) FROM document_chunks"))

    assert hidden == 0
    assert seen == [document["id"]]
    assert chunks and chunks > 0


async def test_the_policy_hides_every_document_when_no_context_is_published(
    platform: Platform, cast: Cast, app_connection: async_sessionmaker
) -> None:
    """A forgotten context reads as "no rows", never as "every row".

    This is the whole design of the second line of defence: the failure mode of a
    missing filter is silence, not disclosure.
    """
    await ready(platform, cast.uploader, text_bytes(), title="Mio")

    async with app_connection() as session:
        documents = await session.scalar(text("SELECT count(*) FROM documents"))
        chunks = await session.scalar(text("SELECT count(*) FROM document_chunks"))

    assert (documents, chunks) == (0, 0)


async def test_a_request_cannot_write_a_chunk(
    platform: Platform, cast: Cast, app_connection: async_sessionmaker
) -> None:
    """Chunks are the pipeline's to write: a request's context writes none.

    Which is what makes a retrieval query safe even if it forgets a filter — reading
    follows the document's visibility and writing is refused outright, so a caller
    cannot plant text in a document they may not open.
    """
    document = await ready(platform, cast.uploader, text_bytes("Contenido."), title="Mio")

    async with app_connection() as session:
        await publish(session, cast.uploader.employee_id, ["low"], [])
        with pytest.raises(Exception) as refusal:
            await session.execute(
                text(
                    "INSERT INTO document_chunks (id, document_id, chunk_index, content, "
                    "token_count, chunking_version) VALUES (:id, :document_id, 99, 'x', 1, 'v1')"
                ),
                {"id": uuid4(), "document_id": document["id"]},
            )

    assert "row-level security" in str(refusal.value).lower()


async def test_the_owners_own_document_is_visible_whatever_it_is_classified(
    platform: Platform, cast: Cast, app_connection: async_sessionmaker
) -> None:
    """§4.2's first clause, at the database layer: ownership has no conditions.

    The context published below names the employee and *no department at all*, and every
    document in this fixture is a personal upload with no department either — so the
    company clause cannot admit anything (there is no company document here) and the
    only thing that can be admitting this row is ownership. The control is the second
    employee's own document, which the same context does not return.
    """
    mine = await ready(platform, cast.uploader, text_bytes("Mio."), title="Mio")
    theirs = await ready(platform, cast.colleague, text_bytes("Suyo."), title="Suyo")

    async with app_connection() as session:
        await publish(session, cast.uploader.employee_id, ["low"], [])
        documents = (
            (await session.execute(text("SELECT id::text FROM documents"))).scalars().all()
        )
        chunks = (
            (await session.execute(text("SELECT document_id::text FROM document_chunks")))
            .scalars()
            .all()
        )

    assert documents == [mine["id"]]
    assert theirs["id"] not in documents
    # The chunks of the document the caller owns are readable, and only those: a
    # retrieval query reads this table, so a chunk visible through a document that is
    # not would be the leak the policy exists to prevent.
    assert set(chunks) == {mine["id"]}
    assert await platform.scalar("SELECT count(*) FROM document_chunks") >= 1


# --- part 9: the schema the migration must keep in step with ----------------


def test_the_chunk_vector_column_is_the_locked_dimension() -> None:
    """§10.3's decision, in the schema and in the application constant.

    A pgvector column's dimension is part of the schema, so the migration keeps a
    literal and this test is what stops it drifting from
    `core.constants.EMBEDDING_DIMENSIONS`. Ticket 32 fills the column; the dimension
    cannot change afterwards without re-embedding every row.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "alembic" / "versions" /
              "20261004_1000_documents.py").read_text(encoding="utf-8")
    assert f"VECTOR_DIMENSIONS = {EMBEDDING_DIMENSIONS}" in source
    assert "USING hnsw (embedding vector_cosine_ops)" in source


async def test_the_real_vector_column_has_the_dimension_and_the_hnsw_index(
    settings: Settings,
) -> None:
    """Themed on `test_database.py`'s smoke-table checks, against the real column.

    The smoke table proved the *choice*; this proves the *decision was spent* on the
    table that will hold the vectors, which is a different claim: a migration that
    created a plain `text` column and indexed nothing would leave the smoke table's
    green checks untouched.
    """
    engine = create_async_engine(settings.test_database_url)
    try:
        async with engine.connect() as connection:
            dimension = await connection.scalar(
                text(
                    "SELECT atttypmod FROM pg_attribute "
                    "WHERE attrelid = to_regclass('document_chunks') AND attname = 'embedding'"
                )
            )
            indexdefs = (
                await connection.execute(
                    text("SELECT indexdef FROM pg_indexes WHERE tablename = 'document_chunks'")
                )
            ).scalars().all()
    finally:
        await engine.dispose()

    assert dimension == EMBEDDING_DIMENSIONS
    hnsw = [definition for definition in indexdefs if "hnsw" in definition]
    assert hnsw, f"no HNSW index among {indexdefs}"
    assert "vector_cosine_ops" in hnsw[0]


async def test_the_database_refuses_a_ready_document_with_no_text(
    platform: Platform, cast: Cast
) -> None:
    """The ticket's "never a silent empty document", as a constraint.

    Not an application check: a constraint, so a future code path — or a console —
    cannot store the state the ticket forbids. The same statement is asserted for a
    `failed` document with no reason beside it.
    """
    ready_without_text = await platform.refused_by_database(
        """
        INSERT INTO documents (id, title, owner_employee_id, department_id, clearance_level,
                               visibility, is_company_kb, tags, language, status, storage_path,
                               content_sha256, filename, media_type, file_size, extracted_chars)
        VALUES (:id, 'Vacio', NULL, :department, 'low', 'company', true, '[]'::jsonb, 'es',
                'ready', 'aa/x.txt', repeat('c', 64), 'x.txt', 'text/plain', 10, 0)
        """,
        {"id": uuid4(), "department": cast.department},
    )
    failed_without_reason = await platform.refused_by_database(
        """
        INSERT INTO documents (id, title, owner_employee_id, department_id, clearance_level,
                               visibility, is_company_kb, tags, language, status, storage_path,
                               content_sha256, filename, media_type, file_size)
        VALUES (:id, 'Fallo', NULL, :department, 'low', 'company', true, '[]'::jsonb, 'es',
                'failed', 'bb/y.txt', repeat('d', 64), 'y.txt', 'text/plain', 10)
        """,
        {"id": uuid4(), "department": cast.department},
    )

    assert "ck_documents_ready_has_text" in ready_without_text
    assert "ck_documents_failed_has_reason" in failed_without_reason


async def test_the_database_refuses_a_company_document_without_a_department(
    platform: Platform, cast: Cast
) -> None:
    """§4.2 reaches a company document through its department. Without one, no clause
    admits it and there is no owner to reach it through — so the row cannot exist."""
    refusal = await platform.refused_by_database(
        """
        INSERT INTO documents (id, title, owner_employee_id, department_id, clearance_level,
                               visibility, is_company_kb, tags, language, status, storage_path,
                               content_sha256, filename, media_type, file_size)
        VALUES (:id, 'Sin departamento', NULL, NULL, 'low', 'company', true, '[]'::jsonb, 'es',
                'processing', 'cc/z.txt', repeat('e', 64), 'z.txt', 'text/plain', 10)
        """,
        {"id": uuid4()},
    )

    assert "ck_documents_company_department" in refusal


async def test_the_database_refuses_a_personal_document_with_no_owner(
    platform: Platform, cast: Cast
) -> None:
    """And the other direction: a personal upload with no owner is a file nobody owns
    and nobody may read, which is not a state the schema will hold."""
    refusal = await platform.refused_by_database(
        """
        INSERT INTO documents (id, title, owner_employee_id, department_id, clearance_level,
                               visibility, is_company_kb, tags, language, status, storage_path,
                               content_sha256, filename, media_type, file_size)
        VALUES (:id, 'Sin dueno', NULL, :department, 'low', 'private', false, '[]'::jsonb, 'es',
                'processing', 'dd/w.txt', repeat('f', 64), 'w.txt', 'text/plain', 10)
        """,
        {"id": uuid4(), "department": cast.department},
    )

    assert "ck_documents_company_owner" in refusal
