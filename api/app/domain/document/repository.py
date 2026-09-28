"""Persistence contract for the document module.

Four things about this interface are load-bearing:

* **Nothing commits.** The service commits once per operation, so a document, its
  chunks and the audit record of who uploaded it land together or not at all. A
  document row with no audit entry is a document nobody can account for.

* **Two kinds of read, and the difference is the whole permission story.** The methods
  that answer *a request* (`page_for`, `by_hash_for`, `get_for`) take a `FilterSpec`
  produced by `access.kernel.filter_for`, and their SQL refuses to run without one —
  a document list that could be called without a filter is the defect this module
  would otherwise have to be trusted not to have. The methods the *job* uses
  (`pending_ids`, `get`) take an id and apply no user filter, because the job is not
  acting for anybody: it is the system's own pass, and the database's row-level policy
  is what bounds even that.

* **`replace_chunks` is delete-then-insert, in one statement pair.** Not an upsert per
  row: a re-run that shortened a document would leave the tail of the previous split
  behind, and "the same document parsed twice" would answer with more chunks than
  either run produced. The unique `(document_id, chunk_index)` is the guarantee
  underneath; this method is what makes it a no-op rather than a collision.

* **`by_hash_for` is permission-scoped, not global.** Duplicate detection asks "do
  *you* already have this file", not "does the company". A global lookup would answer
  the second uploader with the first one's document id — a document they may not be
  allowed to open — which turns an upload endpoint into an existence oracle over
  everybody's private files.
"""

from typing import Protocol
from uuid import UUID

from app.domain.access.kernel import FilterSpec
from app.domain.document.models import (
    ChunkInput,
    Document,
    DocumentChunk,
    DocumentMetadata,
    DocumentPage,
    DocumentStatus,
)


class DocumentRepository(Protocol):
    # --- reads that answer a request ---------------------------------------

    async def page_for(self, spec: FilterSpec, *, limit: int, offset: int) -> DocumentPage:
        """The documents this principal may see, newest first, with the total.

        Takes the spec rather than a principal so the *translation* of §4.2 into SQL
        lives here and the *rule* stays in the kernel. A repository that re-derived
        the clauses from a principal would be the second implementation of the rule
        this module exists to avoid.
        """
        ...

    async def get_for(self, spec: FilterSpec, document_id: UUID) -> Document | None:
        """One document, when the principal may see it. `None` otherwise.

        `None` rather than a refusal, because "there is no such document" and "that
        document is not yours" are the same answer to the caller: telling them apart
        is an existence oracle.
        """
        ...

    async def by_hash_for(self, spec: FilterSpec, content_sha256: str) -> Document | None:
        """The caller's own copy of these bytes, when they already have one.

        Scoped by the same spec as the list, so the duplicate answer can only ever
        name a document the caller could open.
        """
        ...

    # --- reads and writes the job uses --------------------------------------

    async def get(self, document_id: UUID) -> Document | None:
        """One document by id, with no permission filter.

        For the job and for the status endpoint's existence check, both of which
        already hold an id rather than a query. A caller that reaches documents
        *from a request* uses `get_for`.
        """
        ...

    async def pending_ids(self, *, limit: int) -> list[UUID]:
        """Ids of the documents waiting to be parsed, oldest first.

        Ids rather than rows: the job reads each one in its own transaction, so a
        document another worker has already taken is re-read as it is now rather than
        as it was when this list was built.
        """
        ...

    # --- writes -------------------------------------------------------------

    async def create(
        self,
        *,
        title: str,
        owner_employee_id: UUID | None,
        uploaded_by_employee_id: UUID,
        metadata: DocumentMetadata,
        storage_path: str,
        content_sha256: str,
        filename: str,
        media_type: str,
        file_size: int,
    ) -> Document:
        """Write the row in `processing`, before anything has parsed it.

        The status is not a parameter: every document starts `processing`, and a
        caller that could create one `ready` could create a document with no text
        behind it — which is exactly the state the ticket forbids.
        """
        ...

    async def set_status(
        self,
        document_id: UUID,
        status: DocumentStatus,
        *,
        failure_reason: str | None = None,
    ) -> Document:
        """Move the status, and nothing else.

        Used by `reprocess` to put a document back to `processing` before the job
        picks it up. The parsing results travel with `mark_parsed`, so a status write
        can never leave a `ready` document whose text was not stored.
        """
        ...

    async def mark_parsed(
        self,
        document_id: UUID,
        *,
        status: DocumentStatus,
        extracted_chars: int,
        page_count: int | None,
        chunk_count: int,
        failure_reason: str | None,
        chunks: list[ChunkInput],
    ) -> Document:
        """Record one run of the pipeline, in a single statement.

        The text length, the page count, the chunk count, the reason and the status
        describe one event: a row that says `ready` beside a stale count is a row that
        disagrees with itself, and splitting this into two writes is how that happens.
        """
        ...

    async def chunks(self, document_id: UUID, *, limit: int = 200) -> list[DocumentChunk]:
        """The stored chunks, in order. For tests and for ticket 32's retrieval."""
        ...

    async def replace_chunks(self, document_id: UUID, chunks: list[ChunkInput]) -> int:
        """Drop this document's chunks and write these, returning how many landed.

        Delete first, always — including when the new list is empty, which is what a
        failed parse produces. A retry that skipped the delete would double the
        document's chunks, and a retry that skipped the write would keep the previous
        run's.
        """
        ...

    async def employee_exists(self, employee_id: UUID) -> bool: ...

    async def department_exists(self, department_id: UUID) -> bool: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


__all__ = ["DocumentRepository"]
