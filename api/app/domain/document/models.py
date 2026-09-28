"""Document value objects.

Six decisions worth reading before the code, and every one of them is about where a
rule *lives*:

* **A document is the row; its chunks are derived.** `Document` carries what the
  pipeline knows about the *file* — status, where it is stored, its content hash, how
  much text came out — and `DocumentChunk` carries what retrieval reads. Ticket 32
  fills the `embedding` column of those chunks and writes the parent rows the
  self-reference was left for; what the document *is* did not change.

* **`owner_employee_id` is NULL exactly when the document is a company one.** The
  design says so in the schema row, the database enforces it as a CHECK, and the
  access rule's first clause reads it that way: a company document has no owner to be
  "your own". A personal upload always has one, and it is the uploader.

* **A company document has a department, and the CHECK says so.** `is_company_kb`
  with no `department_id` is a document §4.2's second clause can never allow for
  anybody: the department test fails for every caller, and there is no owner to reach
  it through clause 1. Rather than storing a row nobody can read, the database
  refuses it, and `DocumentMetadata.require_coherent` refuses it earlier with a
  message that says which field is missing.

* **`status` is the whole progress model the ticket asks for.** `processing` while
  the job has it, `ready` once text is stored, `failed` with `failure_reason` when
  there is none, `archived` when it is retired. There is no percentage: the pipeline
  has two observable steps, and a progress bar that invented a third would be
  inventing a number. The frontend shows the status as a percentage of those steps —
  which is why the API carries `stage` as well.

* **`extracted_chars` counts characters, not bytes and not tokens.** The question it
  answers is the ticket's: did this document produce anything to retrieve? A byte
  count of the stored file cannot answer it, and a token count is `token_count` on the
  chunk rows — where the tokens actually are, and where ticket 32 counts them with the
  embedding model's own tokenizer.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.domain.document.parsing import NO_TEXT_MESSAGE

#: The statuses a document passes through. Spelled out rather than derived, for the
#: reason `project.models.RECORDABLE_STATUSES` gives: a status added later must be
#: *named* here before anything treats it as one.
PROCESSING = "processing"
READY = "ready"
FAILED = "failed"
ARCHIVED = "archived"


class DocumentStatus(StrEnum):
    """Where a document stands.

    `failed` carries a reason and is not terminal: `reprocess` is what a failed
    parse is answered with, and nothing in the state machine prevents a third
    attempt. `archived` is terminal for the *pipeline* — a retired document is not
    parsed again — and is not part of this ticket's surface beyond being refused.
    """

    PROCESSING = PROCESSING
    READY = READY
    FAILED = FAILED
    ARCHIVED = ARCHIVED


#: Where the pipeline is, as a number the screen can draw. Two steps, because the
#: pipeline has two observable ones; `failed` and `archived` are not on the scale and
#: are reported as their own statuses rather than as a percentage of progress.
STAGES: dict[str, int] = {
    DocumentStatus.PROCESSING: 1,
    DocumentStatus.READY: 2,
}
TOTAL_STAGES = 2

#: The statuses `reprocess` accepts. A document being parsed right now is refused —
#: two parsers writing one row is a race nothing here needs; an archived document is
#: refused because retiring it was the decision to stop.
REPROCESSABLE: frozenset[DocumentStatus] = frozenset(
    {DocumentStatus.READY, DocumentStatus.FAILED}
)

#: The clearance levels, highest last. Documents are classified with these and the
#: kernel ranks them; the tuple exists so a validator can name the three rather than
#: accept any string the database's CHECK would reject later.
CLEARANCE_LEVELS: tuple[str, ...] = ("low", "medium", "high")

#: What a document is for, from the design's `visibility` column. `private` is the
#: default for a personal upload; `company` is what a knowledge-base document gets.
#: The column is stored and returned; which clause of §4.2 applies is decided by
#: `is_company_kb` and the owner, not by this string, which is why nothing in the
#: access path reads it.
VISIBILITIES: tuple[str, ...] = ("private", "department", "company")


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    """Everything a caller states about an upload, and nothing it may not.

    `owner_employee_id` is absent on purpose. The owner is the caller for a personal
    upload and `None` for a company one, and it is the *service* that writes it:
    a body that could name an owner would be a body that could hand somebody else's
    upload away, and the ownership clause of §4.2 is what that would defeat.

    `is_company_kb` is the field that moves a document from one rule to another, so
    it is the one field the service checks an action for (`document.manage`), and the
    database re-checks its coherence with the owner and the department.
    """

    title: str
    department_id: UUID | None = None
    clearance_level: str = "low"
    category: str | None = None
    tags: tuple[str, ...] = ()
    language: str = "es"
    is_company_kb: bool = False
    visibility: str | None = None

    def effective_visibility(self) -> str:
        """What the row stores when the caller named nothing.

        Derived rather than defaulted in the field, because the default depends on
        the other fields: a company document is `company`, and a personal one is
        `private`. A plain `default="private"` would make a company document claim a
        visibility its own flag contradicts.
        """
        if self.visibility:
            return self.visibility
        return "company" if self.is_company_kb else "private"

    def require_coherent(self) -> None:
        """Refuse a metadata set the schema would refuse, saying which field it is.

        The database has the same rules as CHECKs; these exist so the caller gets a
        422 naming the field instead of a 500 from a constraint violation. Both are
        kept deliberately — the check is the message, the constraint is the guarantee.
        """
        from app.core.errors import ErrorCode
        from app.domain.errors import DomainError

        if not self.title.strip():
            raise DomainError(
                ErrorCode.INVALID_REQUEST,
                detail="a document needs a title; the filename is not used as one",
            )
        if self.clearance_level not in CLEARANCE_LEVELS:
            raise DomainError(
                ErrorCode.INVALID_REQUEST,
                detail=(
                    f"clearance_level {self.clearance_level!r} is not one of "
                    f"{list(CLEARANCE_LEVELS)}"
                ),
            )
        if self.effective_visibility() not in VISIBILITIES:
            raise DomainError(
                ErrorCode.INVALID_REQUEST,
                detail=(
                    f"visibility {self.effective_visibility()!r} is not one of "
                    f"{list(VISIBILITIES)}"
                ),
            )
        if self.is_company_kb and self.department_id is None:
            raise DomainError(
                ErrorCode.INVALID_REQUEST,
                detail=(
                    "a company knowledge-base document needs a department: §4.2 reaches "
                    "one through its department, so without it nobody but its owner "
                    "could read it — and a company document has no owner"
                ),
            )
        if len(self.tags) > 20:
            raise DomainError(
                ErrorCode.INVALID_REQUEST,
                detail=f"{len(self.tags)} tags is more than the 20 a document may carry",
            )


@dataclass(frozen=True, slots=True)
class Document:
    """One row of `documents`, as the module reads it.

    The stored `filename` is the *normalised* one (`files.safe_filename`), which is
    what a download is named and what a list shows. The original name as the client
    typed it is deliberately not kept: it is the one form of the name that could carry
    a path or a control character, and there is nothing in the product that needs to
    reproduce it byte for byte.
    """

    id: UUID
    title: str
    owner_employee_id: UUID | None
    department_id: UUID | None
    clearance_level: str
    visibility: str
    is_company_kb: bool
    category: str | None
    tags: tuple[str, ...]
    language: str
    status: DocumentStatus
    #: The key under the storage root, never an absolute path. See `storage.py`.
    storage_path: str
    content_sha256: str
    filename: str
    media_type: str
    file_size: int
    extracted_chars: int
    page_count: int | None
    chunk_count: int
    failure_reason: str | None
    uploaded_by_employee_id: UUID | None
    parsed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_ready(self) -> bool:
        return self.status is DocumentStatus.READY

    @property
    def stage(self) -> int:
        """How far along the two-step scale, for a screen that draws progress.

        A status that is not on the scale reports the last step it reached:
        `failed` reached the parsing step and stopped there, and `archived` is a
        document that was ready once.
        """
        if self.status in STAGES:
            return STAGES[self.status]
        return TOTAL_STAGES if self.status is DocumentStatus.ARCHIVED else 1


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    """One stored chunk — a child retrieval matches, or the parent that gives it context.

    `embedding` is deliberately absent from this value object even though the column is
    written now: nothing reads a vector back through the module (retrieval ranks in
    SQL, where the index is), and a 1536-float list on every row of a listing would be
    1536 floats nothing asked for. Ticket 33 reads the column; this is where it lives.
    """

    id: UUID
    document_id: UUID
    chunk_index: int
    content: str
    token_count: int
    page_from: int | None
    page_to: int | None
    heading_path: str | None
    created_at: datetime
    parent_chunk_id: UUID | None = None
    #: The model that produced this row's vector, or `None` when it has none. Recorded
    #: per row because vectors from two models are not comparable: this is what makes
    #: "re-embed everything from the old model" a query.
    embedding_model: str | None = None


@dataclass(frozen=True, slots=True)
class ChunkInput:
    """A chunk to write: `parsing.Chunk` plus the keys the split and the model set.

    `parent_index` is the position of this row's parent *within the same write*, which
    is what the split knows — it labels children and parents by index rather than by
    uuid, because a uuid is the repository's business. The repository resolves the
    index to the id it generated, and a `parent_index` on a parent is `None`: the
    self-reference means "this fragment's context block", and a parent has none.
    """

    chunk_index: int
    content: str
    token_count: int
    page_from: int | None = None
    page_to: int | None = None
    heading_path: str | None = None
    chunking_version: str = ""
    parent_index: int | None = None
    #: The vector retrieval searches with, on children only. `None` means "not
    #: embedded" — a document that was chunked while the provider was `none`, or one
    #: whose embedding call failed — which is exactly what `WHERE embedding IS NULL`
    #: reports as the re-embed worklist.
    embedding: list[float] | None = None
    #: Which model produced `embedding`. Written together with it, never separately:
    #: a vector whose model is unknown cannot be compared with anything.
    embedding_model: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """What one run of the pipeline produced.

    `failure` is a value and not an exception: a scanned file is an ordinary outcome
    of a pipeline that does not do OCR, and every caller — the job, the audit record,
    `reprocess`'s answer — has to say what happened either way.

    **`embedded` is the second outcome and it is not the same as `ready`.** A document
    whose vectors could not be produced has text, has chunks, is `ready` and is *not*
    searchable by vector — which is a state an operator has to be able to see, because
    the remedy is theirs (set the key, re-run the parse) and a silent one would look
    like a document that simply answers nothing. `embedding_failure` carries the
    sentence for the log.
    """

    document_id: UUID
    status: DocumentStatus
    extracted_chars: int = 0
    page_count: int | None = None
    chunk_count: int = 0
    failure: str | None = None
    embedded: bool = False
    embedding_failure: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is DocumentStatus.READY

    @property
    def failed(self) -> bool:
        """The pipeline ran and produced no text. The complement of `succeeded`.

        Both names exist because both are read: the job counts successes, and a test
        about a scanned file asserts the failure, and a caller that had to write
        `not outcome.succeeded` to mean "this is a scan" would be reading the wrong
        thing out loud.
        """
        return self.status is DocumentStatus.FAILED


@dataclass(frozen=True, slots=True)
class DocumentPage:
    """One page of a list, newest first."""

    items: list[Document] = field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


__all__ = [
    "ARCHIVED",
    "CLEARANCE_LEVELS",
    "FAILED",
    "NO_TEXT_MESSAGE",
    "PROCESSING",
    "READY",
    "REPROCESSABLE",
    "STAGES",
    "TOTAL_STAGES",
    "VISIBILITIES",
    "ChunkInput",
    "Document",
    "DocumentChunk",
    "DocumentMetadata",
    "DocumentPage",
    "DocumentStatus",
    "ParsedDocument",
]
