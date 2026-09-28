"""Document request and response shapes.

Two conventions worth naming, both of them about what a client is allowed to state:

* **The upload is multipart, and everything except the file travels as a form
  field.** A JSON body beside a file part is not a thing HTTP offers, so `tags`
  arrives as a comma-separated string and is split here — the alternative, a base64
  file inside JSON, costs a third of the upload in encoding and makes the 50 MB
  ceiling a lie. The form's fields are declared with `Form(...)` in the router, and
  this module holds the *shapes* they become.

* **`owner_employee_id` is never a request field.** The owner is the caller for a
  personal upload and `None` for a company one; both are decided by the service from
  the action the caller holds. A body that could name an owner is a body that could
  hand somebody else's upload away, and §4.2's first clause is what that defeats.

`stage` and `total_stages` are on the response because the screen draws progress and
the *server* owns what progress means: a client that computed "1 of 2" from the status
string would be a second implementation of the pipeline's shape.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.domain.document.models import TOTAL_STAGES, Document, DocumentPage, DocumentStatus
from app.domain.document.parsing import CHUNKING_VERSION


class DocumentRead(BaseModel):
    """One document as a list or a status read returns it.

    The failure reason travels with the status rather than behind a second request:
    "failed" without a reason is a screen that makes the reader call somebody. The
    reason is the ticket's sentence for a scanned file — `no text extracted; upload a
    text version` — and it is shown as-is, in English, because it is a fact about the
    file rather than interface copy.
    """

    id: UUID
    title: str
    owner_employee_id: UUID | None
    department_id: UUID | None
    clearance_level: str
    visibility: str
    is_company_kb: bool
    category: str | None
    tags: list[str]
    language: str
    status: DocumentStatus
    filename: str
    media_type: str
    file_size: int
    content_sha256: str
    extracted_chars: int
    page_count: int | None
    chunk_count: int
    failure_reason: str | None
    uploaded_by_employee_id: UUID | None
    parsed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    #: How far along the pipeline is, and how many steps there are. Both computed
    #: here rather than in the client, so every screen draws the same bar.
    stage: int
    total_stages: int = TOTAL_STAGES
    chunking_version: str = CHUNKING_VERSION

    @property
    def is_ready(self) -> bool:
        return self.status is DocumentStatus.READY


class DocumentPageRead(BaseModel):
    items: list[DocumentRead]
    total: int
    limit: int
    offset: int


def document_read(document: Document) -> DocumentRead:
    return DocumentRead(
        id=document.id,
        title=document.title,
        owner_employee_id=document.owner_employee_id,
        department_id=document.department_id,
        clearance_level=document.clearance_level,
        visibility=document.visibility,
        is_company_kb=document.is_company_kb,
        category=document.category,
        tags=list(document.tags),
        language=document.language,
        status=document.status,
        filename=document.filename,
        media_type=document.media_type,
        file_size=document.file_size,
        content_sha256=document.content_sha256,
        extracted_chars=document.extracted_chars,
        page_count=document.page_count,
        chunk_count=document.chunk_count,
        failure_reason=document.failure_reason,
        uploaded_by_employee_id=document.uploaded_by_employee_id,
        parsed_at=document.parsed_at,
        created_at=document.created_at,
        updated_at=document.updated_at,
        stage=document.stage,
    )


def page_read(page: DocumentPage) -> DocumentPageRead:
    return DocumentPageRead(
        items=[document_read(document) for document in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


__all__ = ["DocumentPageRead", "DocumentRead", "document_read", "page_read"]
