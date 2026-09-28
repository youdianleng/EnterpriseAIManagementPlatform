"""The document ingestion pipeline.

The interface is fixed by `docs/architecture/codebase-design.md` §2.5:

    ingest(file, metadata, permissions) -> DocumentId
    status_of(document_id)              -> IngestState
    reprocess(document_id)              -> IngestState

`DocumentService` is that interface. Everything else here is behind it: `files` decides
what may be uploaded and what a filename becomes, `parsing` extracts text and splits it,
`storage` keeps the original, `repository` states what persistence the service needs and
`errors`/`models` are its vocabulary.

**No OCR, and that is a decision rather than a gap.** A scanned file produces no text,
which is `failed` with a message telling the reader to upload a text version. Adding OCR
later is a parser adapter inside `parsing.extract` and changes nothing above it — which
is precisely the substitution §2.5 says the interface's shape was chosen to allow.
"""

from app.domain.document.models import (
    ChunkInput,
    Document,
    DocumentChunk,
    DocumentMetadata,
    DocumentPage,
    DocumentStatus,
    ParsedDocument,
)
from app.domain.document.repository import DocumentRepository
from app.domain.document.service import DocumentService, Upload

__all__ = [
    "ChunkInput",
    "Document",
    "DocumentChunk",
    "DocumentMetadata",
    "DocumentPage",
    "DocumentRepository",
    "DocumentService",
    "DocumentStatus",
    "ParsedDocument",
    "Upload",
]
