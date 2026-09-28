"""The document ingestion module: one file in, one state out.

`docs/architecture/codebase-design.md` §2.5 fixes the interface, and this class is it:

    ingest(file, metadata, permissions) -> DocumentId
    status_of(document_id)              -> IngestState
    reprocess(document_id)              -> IngestState

Everything §2.5 lists as hidden is hidden behind those three: extraction, the
structural split, the parent/child chunk rows, failure classification, and the
idempotence a retry depends on. A future OCR adapter is a change to
`parsing.extract` and to nothing in this file — which is the property the interface was
shaped for.

Five decisions worth reading, because each of them is a rule rather than a mechanism:

* **Permissions are the principal, and every read and write goes through the kernel.**
  `ingest` asks `can()` for the actions the upload performs and derives the identity of
  the row (owner, or company) from them; every read uses `filter_for(principal,
  DOCUMENT)` and hands the resulting `FilterSpec` to the repository, which renders it
  as SQL. There is no second document rule anywhere in this module — not a role list,
  not a clearance comparison against a row, not a "managers may also". The one
  comparison this file makes is `requested level <= the caller's own`, and it exists
  because the clearance ceiling is *also* what makes an over-classified upload
  invisible to its own uploader.

* **A company document is administration's to create.** `is_company_kb` moves a
  document from §4.2's ownership clause to its department clause: it stops being
  somebody's file and becomes the organisation's. That is `document.manage`, and the
  check is asked here rather than in the router because the *same* route serves both
  kinds — the caller chooses which by what they send.

* **`reprocess` clears the chunks before the job re-parses.** The *removal* is
  synchronous and happens in this transaction; the re-parse is the job's, exactly as
  the first parse was. So a retry can never observe two splits of the same document,
  and a failed re-parse leaves the document with no chunks rather than with a stale
  set that no longer matches its own text.

* **The status is the progress model, and the job writes it.** `ingest` writes
  `processing` and returns; the request does not wait. That is the ticket's
  requirement — "HTTP 请求不等待解析完成" — and it is why this method has no code path
  that parses anything.

* **Failure is a value, not an exception.** A scanned file is an ordinary outcome of a
  pipeline with no OCR in it. `parse_document` returns a `ParsedDocument` whose
  `status` says what happened and whose `failure` carries the ticket's sentence, so the
  job, the audit record and `reprocess`'s answer all read the same three fields.
"""

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction, record
from app.domain.access.kernel import (
    CLEARANCE_RANK,
    Action,
    ResourceKind,
    can,
    filter_for,
)
from app.domain.access.permissions import DOCUMENT_CROSS_DEPARTMENT_ROLES
from app.domain.access.principal import Principal
from app.domain.document.errors import DocumentErrorCode
from app.domain.document.files import MAX_UPLOAD_BYTES, AcceptedFile, accept
from app.domain.document.models import (
    REPROCESSABLE,
    ChunkInput,
    Document,
    DocumentMetadata,
    DocumentPage,
    DocumentStatus,
    ParsedDocument,
)
from app.domain.document.parsing import CHUNKING_VERSION, ParseRefused, parse
from app.domain.document.repository import DocumentRepository
from app.domain.document.storage import FileStore, content_digest
from app.domain.errors import DomainError

#: The audit trail's entity type. One string, so "everything that happened to this
#: document" is an equality filter rather than a list.
ENTITY_TYPE = "document"

#: How many documents one pass of the job takes. A batch size rather than a limit on
#: the work: the job is run again immediately, and a thousand documents in one
#: transaction is a transaction that holds locks for minutes.
DEFAULT_BATCH = 100

#: The setting that says "this session is the system, not a user". It is the same name
#: migration 0021's policies read, and it is the reason the parsing job can see the
#: documents it is asked to parse at all: the row-level policy on `documents` admits a
#: caller's own rows and their department's, and the job is neither.
SYSTEM_SETTING = "app.current_system"


async def publish_system_context(session: AsyncSession) -> None:
    """Declare this session the pipeline's, before it reads or writes a document.

    `set_config(..., is_local => true)` scopes the flag to the transaction, exactly as
    `apply_rls_context` scopes a principal's — so it cannot outlive the pass that set
    it, and a pooled connection that serves a request afterwards carries nothing.

    **Called by `jobs/parse_documents` and by nothing else.** A request that called it
    would be a request that had switched off the second line of defence, and the reason
    it is a function here rather than a `set_config` inlined in the job is so that the
    one place it happens is greppable.
    """
    from sqlalchemy import text

    await session.execute(
        text("SELECT set_config(:name, 'true', true)"), {"name": SYSTEM_SETTING}
    )


@dataclass(frozen=True, slots=True)
class Upload:
    """What the router hands over: the bytes, the name they arrived under, and what
    the client said about them.

    A value object rather than three parameters, because the service's interface is
    fixed at `ingest(file, metadata, permissions)` and this is the `file` half of it —
    one thing that came in, named and typed.
    """

    content: bytes
    filename: str | None


class DocumentService:
    """Ingestion, status, retrieval and retry, for one caller.

    `principal` is the caller. It is required rather than optional for the reason
    `TimesheetService` records: a service that could be built without one would be a
    service that could read anybody's documents, since the kernel is the only thing
    that decides which ones are whose.
    """

    def __init__(
        self,
        repository: DocumentRepository,
        session: AsyncSession,
        *,
        principal: Principal,
        storage: FileStore,
        max_upload_bytes: int = MAX_UPLOAD_BYTES,
    ) -> None:
        self._repository = repository
        self._session = session
        self._principal = principal
        self._storage = storage
        self._max_upload_bytes = max_upload_bytes

    @property
    def specs(self) -> object:
        """The filter this principal's document reads are bounded by.

        Exposed because it is the *evidence* of the rule rather than a convenience:
        `tests/test_documents.py` asserts the same spec the queries use, so a read
        added without one fails a test rather than a review.
        """
        return filter_for(self._principal, ResourceKind.DOCUMENT)

    # --- the interface §2.5 fixes -------------------------------------------

    async def ingest(self, file: Upload, metadata: DocumentMetadata) -> Document:
        """Keep the file, write the row in `processing`, and answer with it.

        **Returns immediately, and parses nothing.** The parsing is the job's; this
        method's whole job is to make the document exist and be findable, which is why
        a 50 MB upload costs one hash, one write and one insert.
        """
        accepted = self._require_supported(file)
        content = self._require_size(file)
        metadata.require_coherent()
        await self._require_upload_allowed(metadata)

        digest = content_digest(content)
        existing = await self._repository.by_hash_for(self.specs, digest)  # type: ignore[arg-type]
        if existing is not None:
            raise DomainError(
                DocumentErrorCode.DOCUMENT_DUPLICATE,
                detail=(
                    f"{metadata.title!r} has the same content as document {existing.id} "
                    f"({existing.title!r}), which this caller can already read"
                ),
            )
        await self._require_uploader()

        key = self._storage.put(content, extension=accepted.extension)
        document = await self._create(
            title=metadata.title, metadata=metadata, accepted=accepted,
            key=key, digest=digest, size=len(content),
        )
        await record(
            self._session,
            action=AuditAction.DOCUMENT_UPLOADED,
            entity_type=ENTITY_TYPE,
            entity_id=document.id,
            after={
                "title": document.title,
                "owner_employee_id": document.owner_employee_id,
                "department_id": document.department_id,
                "clearance_level": document.clearance_level,
                "is_company_kb": document.is_company_kb,
                "filename": document.filename,
                "media_type": document.media_type,
                "file_size": document.file_size,
                "content_sha256": document.content_sha256,
                "storage_path": document.storage_path,
                # The status is in the trail because it is what the request answered
                # with: "processing" here is the fact that the parse had not happened
                # yet when the uploader was told the upload succeeded.
                "status": document.status,
            },
        )
        await self._repository.commit()
        return document

    async def status_of(self, document_id: UUID) -> Document:
        """The document's state, when the caller may see it at all.

        A document the caller cannot read is `DOCUMENT_NOT_FOUND`, not a refusal: the
        two answers are the same to the client, and telling them apart would make this
        endpoint an existence oracle over everybody's private uploads.
        """
        document = await self._repository.get_for(self.specs, document_id)  # type: ignore[arg-type]
        if document is None:
            raise DomainError(
                DocumentErrorCode.DOCUMENT_NOT_FOUND,
                detail=f"no document {document_id} in this caller's reach",
            )
        return document

    async def reprocess(self, document_id: UUID) -> Document:
        """Clear the chunks and put the document back in the queue.

        Idempotent by construction: the chunks are deleted here, the parse happens in
        the job, and running this twice leaves the document in `processing` with the
        same rows it had after the first call. Nothing is duplicated because there is
        nothing to duplicate — the previous split is gone before the next one starts.
        """
        document = await self.status_of(document_id)
        if document.status not in REPROCESSABLE:
            raise DomainError(
                DocumentErrorCode.DOCUMENT_REPROCESS_UNSUPPORTED,
                detail=(
                    f"document {document_id} is {document.status}; only a ready or failed "
                    "document is re-parsed"
                ),
            )
        if not await self._repository.employee_exists(self._principal.employee_id):
            raise DomainError(
                DocumentErrorCode.DOCUMENT_NOT_FOUND,
                detail=f"unknown employee {self._principal.employee_id}",
            )

        await self._repository.replace_chunks(document_id, [])
        moved = await self._repository.set_status(document_id, DocumentStatus.PROCESSING)
        await self._repository.commit()
        return moved

    # --- retrieval ----------------------------------------------------------

    async def download(self, document_id: UUID) -> tuple[Document, bytes]:
        """The stored original, when the caller may read the document.

        **The same rule the read uses, asked once.** There is no second check here and
        no separate `document.download` action: a caller who may read a document may
        read the file it came from, because a citation that cannot be opened is not a
        citation. The lookup goes through `get_for`, so the permission decision is the
        repository's translation of the kernel's own spec.
        """
        document = await self.status_of(document_id)
        try:
            return document, self._storage.get(document.storage_path)
        except FileNotFoundError as error:
            # The row says there is a file and the storage root disagrees. Its own
            # code, because this is a data error an operator acts on rather than a
            # permission refusal the client can do something about.
            raise DomainError(
                DocumentErrorCode.DOCUMENT_FILE_MISSING,
                detail=f"document {document_id} names {document.storage_path!r}: {error}",
            ) from error

    async def list_documents(self, *, limit: int = 50, offset: int = 0) -> DocumentPage:
        """The documents this caller may see, newest first.

        The query is bounded by the spec and there is no parameter that could widen
        it, which is `filter_for`'s whole purpose: a list endpoint that forgot its
        filter would be a list of everybody's uploads, and there is no way to write
        this one that forgets.
        """
        return await self._repository.page_for(self.specs, limit=limit, offset=offset)  # type: ignore[arg-type]

    # --- the parsing half, called by the job --------------------------------

    async def parse_document(self, document_id: UUID) -> ParsedDocument:
        """Read the stored file, split it, and write the outcome.

        **One document, one transaction.** The caller — the job — opens a session per
        document, so a file that cannot be read stops one document and not the batch,
        and a crash half way through leaves every earlier document committed.

        Deliberately unfiltered: the job is not acting for a user, and a document
        being parsed belongs to whoever uploaded it. What bounds this method is the
        database's own row-level policy, which the job's connection is subject to like
        every other.

        A `ParseRefused` is an outcome and not an error: the document goes to `failed`
        with the ticket's message and no chunks, which is the state `reprocess` exists
        to leave. Any other exception is the *job's* problem and propagates, so a bug
        in this pipeline is a loud failure rather than a document quietly marked
        failed.
        """
        document = await self._repository.get(document_id)
        if document is None:
            raise DomainError(
                DocumentErrorCode.DOCUMENT_NOT_FOUND, detail=f"no document {document_id}"
            )
        if document.status is DocumentStatus.ARCHIVED:
            raise DomainError(
                DocumentErrorCode.DOCUMENT_REPROCESS_UNSUPPORTED,
                detail=f"document {document_id} is archived; a retired document is not parsed",
            )

        try:
            content = self._storage.get(document.storage_path)
        except FileNotFoundError as error:
            return await self._failed(
                document, f"the stored original is missing: {error}", audit=False
            )

        try:
            parsed, chunks = parse(content, document.media_type)
        except ParseRefused as refusal:
            return await self._failed(document, str(refusal))

        written = [
            ChunkInput(
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                token_count=chunk.token_count,
                page_from=chunk.page_from,
                page_to=chunk.page_to,
                heading_path=chunk.heading_path,
                chunking_version=CHUNKING_VERSION,
            )
            for chunk in chunks
        ]
        updated = await self._repository.mark_parsed(
            document_id,
            status=DocumentStatus.READY,
            extracted_chars=parsed.char_count,
            page_count=parsed.page_count,
            chunk_count=len(written),
            failure_reason=None,
            chunks=written,
        )
        await record(
            self._session,
            action=AuditAction.DOCUMENT_PARSED,
            entity_type=ENTITY_TYPE,
            entity_id=document_id,
            # The job is not acting for a user, and it must not be attributed to one.
            # `record()` fills whatever the caller omits from the request context, and
            # that context is a *process* variable: the uploader's `actor_user_id` is
            # still bound long after their request finished, so a parse that let
            # `record()` guess would name the person who uploaded the file as the one
            # who parsed it. Stated here, explicitly, as the system's own act.
            actor_user_id=None,
            actor_roles=frozenset(),
            ip_address=None,
            user_agent=None,
            initiated_by="system",
            after={
                "status": updated.status,
                "extracted_chars": updated.extracted_chars,
                "page_count": updated.page_count,
                "chunk_count": updated.chunk_count,
                "chunking_version": CHUNKING_VERSION,
            },
            reason="parse succeeded",
        )
        return ParsedDocument(
            document_id=document_id,
            status=updated.status,
            extracted_chars=updated.extracted_chars,
            page_count=updated.page_count,
            chunk_count=updated.chunk_count,
        )

    async def pending_documents(self, *, limit: int = DEFAULT_BATCH) -> list[UUID]:
        """What is waiting to be parsed, oldest first."""
        return await self._repository.pending_ids(limit=limit)

    # --- internals ----------------------------------------------------------

    async def _failed(
        self, document: Document, reason: str, *, audit: bool = True
    ) -> ParsedDocument:
        """`failed`, with the reason, and no chunks at all.

        The chunks are cleared here as well as in `reprocess`, because a document that
        was ready and is now failing — a re-parse of a file whose storage vanished —
        must not answer retrieval with its previous text while claiming to have none.
        """
        updated = await self._repository.mark_parsed(
            document.id,
            status=DocumentStatus.FAILED,
            extracted_chars=0,
            page_count=None,
            chunk_count=0,
            failure_reason=reason,
            chunks=[],
        )
        if audit:
            await record(
                self._session,
                action=AuditAction.DOCUMENT_PARSE_FAILED,
                entity_type=ENTITY_TYPE,
                entity_id=document.id,
                # The system's act, as above: the bound request context belongs to
                # whoever uploaded the file and has nothing to do with this pass.
                actor_user_id=None,
                actor_roles=frozenset(),
                ip_address=None,
                user_agent=None,
                initiated_by="system",
                after={
                    "status": updated.status,
                    "failure_reason": updated.failure_reason,
                    "media_type": updated.media_type,
                    "filename": updated.filename,
                },
                reason=reason,
            )
        return ParsedDocument(
            document_id=document.id,
            status=updated.status,
            failure=updated.failure_reason,
        )

    async def _create(
        self,
        *,
        title: str,
        metadata: DocumentMetadata,
        accepted: AcceptedFile,
        key: str,
        digest: str,
        size: int,
    ) -> Document:
        """Write the row, turning the duplicate constraint into the catalogued answer.

        The application's own duplicate check has already run, so reaching the
        constraint means a second request for the same bytes was in flight. The
        unique index keyed on the owner is what makes that a fact; this catches it and
        answers with the same code the first check would have, rather than a 500.
        """
        owner = None if metadata.is_company_kb else self._principal.employee_id
        try:
            return await self._repository.create(
                title=title,
                owner_employee_id=owner,
                uploaded_by_employee_id=self._principal.employee_id,
                metadata=metadata,
                storage_path=key,
                content_sha256=digest,
                filename=accepted.filename,
                media_type=accepted.media_type,
                file_size=size,
            )
        except IntegrityError as error:
            await self._repository.rollback()
            existing = await self._repository.by_hash_for(self.specs, digest)  # type: ignore[arg-type]
            if existing is not None:
                raise DomainError(
                    DocumentErrorCode.DOCUMENT_DUPLICATE,
                    detail=f"same content as document {existing.id} ({error.orig})",
                ) from error
            raise

    def _require_supported(self, file: Upload) -> AcceptedFile:
        accepted = accept(file.filename)
        if accepted is None:
            raise DomainError(
                DocumentErrorCode.DOCUMENT_UPLOAD_TYPE_UNSUPPORTED,
                detail=(
                    f"{Path(file.filename or '').name!r} is not one of the accepted "
                    f"formats (PDF, DOCX, XLSX, TXT, Markdown)"
                ),
            )
        return accepted

    def _require_size(self, file: Upload) -> bytes:
        if not file.content:
            raise DomainError(
                DocumentErrorCode.DOCUMENT_UPLOAD_EMPTY, detail="the upload carried no bytes"
            )
        if len(file.content) > self._max_upload_bytes:
            raise DomainError(
                DocumentErrorCode.DOCUMENT_UPLOAD_TOO_LARGE,
                detail=(
                    f"{len(file.content)} bytes is over the {self._max_upload_bytes}-byte "
                    "ceiling; the file is refused rather than truncated"
                ),
            )
        return file.content

    async def _require_uploader(self) -> None:
        if not await self._repository.employee_exists(self._principal.employee_id):
            raise DomainError(
                DocumentErrorCode.DOCUMENT_NOT_FOUND,
                detail=f"unknown employee {self._principal.employee_id}",
            )

    async def _require_upload_allowed(self, metadata: DocumentMetadata) -> None:
        """The questions an upload asks, each with the action or the fact it turns on.

        **May this role upload at all** is `document.upload`. **May it file into that
        department** is the same question §4.2's second clause asks of a reader — with
        one difference that administration is the reason for: a *company* document is
        filed *by* the roles that maintain the knowledge base, and those roles reach
        every department by definition (`DOCUMENT_CROSS_DEPARTMENT_ROLES` plus the two
        that hold `document.manage`). Requiring an administrator to be assigned to a
        department before they can file a policy into it would be requiring the
        permission they already hold to be expressed as a position. A *personal* upload
        is different: it is filed into the uploader's own reach and nowhere else.
        **May it classify that high** is the ceiling, below. And **may it create a
        company document** is `document.manage`, because that is what turns a personal
        file into the organisation's.
        """
        decision = can(self._principal, Action.DOCUMENT_UPLOAD)
        if decision.denied:
            raise DomainError(
                DocumentErrorCode.FORBIDDEN,
                detail=f"document.upload refused: {decision.primary_reason} ({decision.detail})",
            )

        if metadata.department_id is not None:
            if not await self._repository.department_exists(metadata.department_id):
                raise DomainError(
                    DocumentErrorCode.INVALID_REQUEST,
                    detail=f"unknown department {metadata.department_id}",
                )
            reachable = self._principal.covers_department(metadata.department_id) or (
                metadata.is_company_kb
                and (
                    bool(self._principal.roles & DOCUMENT_CROSS_DEPARTMENT_ROLES)
                    or can(self._principal, Action.DOCUMENT_MANAGE).allowed
                )
            )
            if not reachable:
                raise DomainError(
                    DocumentErrorCode.FORBIDDEN,
                    detail=(
                        f"department {metadata.department_id} is outside the caller's scope; "
                        "a personal document must not be filed where its author could not "
                        "read it"
                    ),
                )

        if not self._clearance_within_reach(metadata):
            raise DomainError(
                DocumentErrorCode.FORBIDDEN,
                detail=(
                    f"clearance {metadata.clearance_level!r} is above the caller's own "
                    f"({self._principal.clearance_level!r}); classifying a document above "
                    "your own level hides it from you"
                ),
            )

        if metadata.is_company_kb:
            manage = can(self._principal, Action.DOCUMENT_MANAGE)
            if manage.denied:
                raise DomainError(
                    DocumentErrorCode.FORBIDDEN,
                    detail=(
                        "document.manage refused: only administration and HR create a "
                        f"company knowledge-base document ({manage.primary_reason})"
                    ),
                )

    def _clearance_within_reach(self, metadata: DocumentMetadata) -> bool:
        """`rank(requested) <= rank(caller)`, read from the kernel's own ladder.

        The one comparison this module makes, and it is not a second copy of the
        access rule: §4.2's ceiling is the kernel's, and this asks it about the *level*
        rather than about a row. There is no override, not even for the roles that may
        classify: `document.set_clearance` (ticket 36) is the action for raising a
        level on a document that already exists, and it is deliberately not a way to
        file something above your own ceiling — an upload that its own uploader could
        not open is a document nobody can maintain.
        """
        caller = CLEARANCE_RANK.get(self._principal.clearance_level, 0)
        requested = CLEARANCE_RANK.get(metadata.clearance_level, 0)
        return requested <= caller


__all__ = ["DEFAULT_BATCH", "ENTITY_TYPE", "DocumentService", "Upload"]
