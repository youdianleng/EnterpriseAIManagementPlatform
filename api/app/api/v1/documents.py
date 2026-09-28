"""Document endpoints: upload, list, status, download, reprocess.

Five routes, and the two design decisions a reader should have in mind:

**Upload answers immediately with the document in `processing`.** The parsing is
`app.jobs.parse_documents`'s, and this route schedules nothing — it hashes the bytes,
writes the file and the row, and returns. That is the ticket's "HTTP 请求不等待解析完成"
expressed as an absence: there is no code path here that could wait, because the
service this route builds cannot parse anything.

**A duplicate is a 409 carrying the existing document's id, not an idempotent 200.**
Both would satisfy "同一文件重复上传通过内容校验和被识别并提示，不静默产生重复文档", so
the choice is made on what the two answers *mean*:

* An idempotent 200 says "this request has already been carried out", which is true of
  a retried request and false of a second deliberate upload. Conflating them means an
  uploader who meant to file the same policy under a second department is told
  everything is fine while nothing happened to their new metadata.
* A 201-with-the-old-id says "created" while creating nothing, and a client that
  keyed on the id would overwrite its own local copy of the title, department and
  tags with the first upload's — silently undoing the metadata the second request
  carried.
* A 409 is a *conflict with existing state*, which is exactly what this is, and its
  body carries `existing_document_id` so the client can offer the one useful action:
  open the document you already have. The message key
  (`errors.document_duplicate`) is bilingual like every other refusal, so the prompt is
  readable in both languages without the client inventing a sentence.

**The download is guarded by the *same* rule the read is**, not a second one: it asks
the repository for the document through the kernel's spec (`filter_for(...,
DOCUMENT)`) and returns the file it names. There is deliberately no `document.download`
action — a citation that cannot be opened is not a citation, and a caller who may read
a document may read the file it came from. An administrator is not an exception: the
second clause of §4.2 is the whole rule for somebody else's upload.

**The file is read in chunks with a ceiling, and refused rather than truncated.** A
50 MB-plus upload is stopped as soon as the limit is passed rather than buffered and
then refused, so the memory a request costs is bounded by the limit and not by what a
client chose to send.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import current_principal, db_session, require
from app.api.v1.schemas.document import DocumentPageRead, DocumentRead, document_read, page_read
from app.core.errors import AppError, ErrorCode
from app.domain.access import Action, Principal
from app.domain.access.kernel import ResourceKind
from app.domain.document.models import DocumentMetadata
from app.domain.document.service import DocumentService, Upload
from app.repositories.document import PostgresDocumentRepository

router = APIRouter(prefix="/documents", tags=["documents"])

#: Everyone, about what they may reach. *Which* documents is §4.2's question, asked
#: by the service through the kernel — the route-level guard is the role check.
read_documents = require(Action.DOCUMENT_READ, ResourceKind.DOCUMENT)
list_documents = require(Action.DOCUMENT_LIST, ResourceKind.DOCUMENT)
upload_document = require(Action.DOCUMENT_UPLOAD, ResourceKind.DOCUMENT)

#: How much of the body is read before the ceiling is applied. One byte over the
#: limit is enough to know the file is too large, and reading further would be
#: buffering an upload this request has already decided to refuse.
_CHUNK = 1024 * 1024


def _service(session: AsyncSession, principal: Principal) -> DocumentService:
    """The module, wired to its repository, its storage root and its embedder.

    The storage root and the embedding provider both come from settings and are read
    here rather than inside the service, so a test can hand the same service an
    in-memory store and a recording embedder and exercise the whole pipeline without a
    volume and without a network. Those are the seams `docs/architecture/
    codebase-design.md` §4 calls real ones — the second adapter of each is in
    `tests/support/documents.py`.
    """
    from app.config import get_settings
    from app.domain.document.embeddings import build_embedder
    from app.domain.document.storage import LocalFileStore

    settings = get_settings()
    return DocumentService(
        PostgresDocumentRepository(session),
        session,
        principal=principal,
        storage=LocalFileStore(settings.document_storage_path),
        max_upload_bytes=settings.document_max_upload_bytes,
        embedder=build_embedder(
            settings.embeddings_provider,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
        ),
    )


async def _read_upload(file: UploadFile, ceiling: int) -> bytes:
    """The upload's bytes, or a refusal as soon as the ceiling is passed.

    `UploadFile` is a spooled temporary file, so reading it in pieces is what keeps a
    request's memory bounded: the ceiling plus one chunk is the most this ever holds.
    """
    limit = ceiling + 1
    buffer = bytearray()
    while True:
        chunk = await file.read(_CHUNK)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > ceiling:
            raise AppError(
                ErrorCode.DOCUMENT_UPLOAD_TOO_LARGE,
                detail=(
                    f"the upload is over the {ceiling}-byte ceiling; refused after "
                    f"{len(buffer)} bytes rather than truncated"
                ),
            )
        if len(buffer) > limit:  # pragma: no cover - the branch above always wins
            break
    return bytes(buffer)


def _tags(raw: str | None) -> tuple[str, ...]:
    """`"payroll, 2026, policy"` → `("payroll", "2026", "policy")`.

    Split, trimmed and de-duplicated, and empties dropped: a client that sends
    `"a,,a"` means one tag, and storing `["a", "", "a"]` would make "everything tagged
    a" a query with three answers for one document.
    """
    if not raw:
        return ()
    seen: list[str] = []
    for tag in raw.split(","):
        cleaned = tag.strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return tuple(seen)


@router.get(
    "",
    response_model=DocumentPageRead,
    summary="List the documents you may read",
    dependencies=[Depends(list_documents)],
)
async def list_documents_route(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> DocumentPageRead:
    """Your own documents, your department's, and the company's within your clearance.

    Which ones those are is the kernel's answer, translated into SQL by the
    repository; this route has no parameter that could widen it. The total is returned
    with the page so a client never counts by repeating a filter it cannot see.
    """
    page = await _service(session, principal).list_documents(limit=limit, offset=offset)
    return page_read(page)


@router.post(
    "",
    response_model=DocumentRead,
    status_code=201,
    summary="Upload a document; parsing happens in the background",
    dependencies=[Depends(upload_document)],
)
async def upload_document_route(
    file: UploadFile = File(description="PDF, DOCX, XLSX, TXT or Markdown, at most 50 MB"),
    title: str = Form(min_length=1, max_length=300),
    department_id: UUID | None = Form(default=None),
    clearance_level: str = Form(default="low"),
    category: str | None = Form(default=None, max_length=120),
    tags: str | None = Form(default=None, description="Comma-separated"),
    language: str = Form(default="es", max_length=8),
    is_company_kb: bool = Form(default=False),
    visibility: str | None = Form(default=None),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> DocumentRead:
    """Keep the original, record it in `processing`, and answer.

    **201 with the row in `processing`**, always: the parse is the job's, and a caller
    that wanted to wait for it would be waiting on a process this request does not
    start. The response carries `stage` and `total_stages` so the screen can draw the
    progress the status means without knowing the pipeline's shape.

    A second upload of the same bytes is a 409 naming the document the caller already
    has — see this module's docstring for why that is the answer rather than an
    idempotent 200.
    """
    from app.config import get_settings

    settings = get_settings()
    content = await _read_upload(file, settings.document_max_upload_bytes)
    metadata = DocumentMetadata(
        title=title.strip(),
        department_id=department_id,
        clearance_level=clearance_level,
        category=category.strip() if category else None,
        tags=_tags(tags),
        language=language,
        is_company_kb=is_company_kb,
        visibility=visibility,
    )
    document = await _service(session, principal).ingest(
        Upload(content=content, filename=file.filename), metadata
    )
    return document_read(document)


@router.get(
    "/{document_id}",
    response_model=DocumentRead,
    summary="Read one document's metadata and status",
    dependencies=[Depends(read_documents)],
)
async def read_document_route(
    document_id: UUID,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> DocumentRead:
    """The document, or a 404 when it is outside this caller's reach.

    The failure reason travels with the status, so a screen that shows `failed` can
    show why without a second request.
    """
    return document_read(await _service(session, principal).status_of(document_id))


@router.get(
    "/{document_id}/content",
    summary="Download the original file this document was uploaded from",
    dependencies=[Depends(read_documents)],
)
async def download_document_route(
    document_id: UUID,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> Response:
    """The stored original, under the rule that governs reading the document.

    **The same rule, not a second one.** The lookup is the read's — `filter_for`'s
    spec, rendered by the repository — so there is no `document.download` action and
    no role list here to drift from §4.2. A caller the kernel refuses is a 404, which
    is the same answer an unknown id gets.

    `attachment` rather than `inline`: a stored PDF rendered in the browser would run
    the client's PDF viewer against a file the server has not inspected, and the point
    of this endpoint is to hand back the document the citations point at.
    """
    document, content = await _service(session, principal).download(document_id)
    return Response(
        content=content,
        media_type=document.media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{_header_safe(document.filename)}"'
            ),
            "Content-Length": str(len(content)),
            # The hash is the row's, so a client can verify that what it received is
            # what was uploaded — which is the whole reason the column exists.
            "X-Document-SHA256": document.content_sha256,
        },
    )


@router.post(
    "/{document_id}/reprocess",
    response_model=DocumentRead,
    summary="Re-run parsing for a document whose text is missing or wrong",
    dependencies=[Depends(read_documents)],
)
async def reprocess_document_route(
    document_id: UUID,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> DocumentRead:
    """Clear the stored chunks and put the document back in the queue.

    The re-parse itself is the job's, exactly as the first parse was, so this answers
    with the document in `processing`. Nothing is duplicated: the previous split is
    deleted in the same transaction that moves the status, so a run that fails leaves
    the document with no chunks rather than with a stale set.
    """
    return document_read(await _service(session, principal).reprocess(document_id))


def _header_safe(name: str) -> str:
    """A filename that cannot break the header it travels in.

    The stored name is already one harmless segment (`files.safe_filename`), so this
    is the second belt: quotes and non-ASCII are stripped, because a header is not the
    place to discover that a name contained one.
    """
    cleaned = "".join(
        character
        for character in name
        if character.isascii() and character not in '"\\'
    )
    return cleaned or "document"


__all__ = ["router"]
