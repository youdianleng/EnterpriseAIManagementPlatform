"""ORM rows for the documents module: the file, and the chunks retrieval reads.

Two tables, and the shape is fixed by `docs/DESIGN.md` §3.6 with three deliberate
differences, each of which the service and the kernel depend on:

* **`documents` carries the file's columns directly** — `storage_path`,
  `content_sha256`, `file_size`, `filename`, `media_type`, `page_count` — where the
  design puts them on `document_versions`. A version chain is what `Q36`'s "reference
  back to the original" will need once a document can be *replaced*, and ticket 31
  uploads files rather than revising them: a second table holding exactly one row per
  document would be a join in every query for a feature nobody can reach yet. The
  version table arrives with the ticket that versions something, and moves these
  columns then.

* **`current_version_id` is absent.** It has nothing to point at while there is one
  version; `chunking_version` and the embedding model live on `document_chunks` in
  this implementation, where the rows they describe are, rather than one level up where
  they would have to be trusted to agree with every chunk beneath them.

* **`embedding` and `embedding_model` are written together and never apart.** §10.3
  fixes the dimension at 1536 and pgvector cannot change a column's dimension in
  place, so the column and its HNSW index existed from ticket 31; ticket 32 fills them
  — on the *child* rows, which is what retrieval searches — and records which model
  produced each vector beside it, because two models' vectors are not comparable and a
  re-embed has to be able to find the stale ones.

Constraints worth reading: the three CHECKs state what a row *is* (`personal iff
owner`, `company iff a department`, `ready iff text`), and the partial unique index on
`(owner_employee_id, content_sha256)` is what makes "the same file twice" a database
fact for personal uploads — the duplicate rule is scoped to a caller, which is exactly
what an owner-keyed index expresses.
"""

from datetime import datetime
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.constants import EMBEDDING_DIMENSIONS
from app.db_metadata import Base


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    #: NULL for a company document. The kernel's first clause is ownership, so a
    #: company document is reached through its department or an exception role.
    owner_employee_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"), nullable=True
    )
    department_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=True
    )
    clearance_level: Mapped[str] = mapped_column(String(6), nullable=False)
    visibility: Mapped[str] = mapped_column(String(12), nullable=False)
    is_company_kb: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: JSONB, per the design row: a list of labels rather than a comma-joined string,
    #: so "everything tagged payroll" is an index-able containment query and a tag
    #: cannot contain the separator.
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="es")
    status: Mapped[str] = mapped_column(String(12), nullable=False)

    # --- the stored original ------------------------------------------------
    #: A key under the storage root, never an absolute path (see `storage.py`).
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # --- what the pipeline produced ----------------------------------------
    extracted_chars: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    uploaded_by_employee_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "clearance_level IN ('low', 'medium', 'high')", name="ck_documents_clearance"
        ),
        CheckConstraint(
            "visibility IN ('private', 'department', 'company')", name="ck_documents_visibility"
        ),
        # The design's own annotation: `owner_employee_id` is null exactly when the
        # document is the company's. Stated as an equivalence rather than two
        # implications, because both directions have a failure the other misses — a
        # company document with an owner is a private file claiming to be public, and
        # a personal document without one is a file only the exception roles can read.
        CheckConstraint(
            "(owner_employee_id IS NULL) = is_company_kb", name="ck_documents_company_owner"
        ),
        # §4.2 reaches a company document through its department. One without a
        # department is readable by nobody, so it cannot be stored.
        CheckConstraint(
            "NOT is_company_kb OR department_id IS NOT NULL", name="ck_documents_company_department"
        ),
        CheckConstraint(
            "status IN ('processing', 'ready', 'failed', 'archived')", name="ck_documents_status"
        ),
        CheckConstraint("file_size > 0", name="ck_documents_file_size"),
        CheckConstraint("length(content_sha256) = 64", name="ck_documents_sha256"),
        CheckConstraint("extracted_chars >= 0", name="ck_documents_extracted"),
        CheckConstraint("chunk_count >= 0", name="ck_documents_chunks"),
        # "ready" is a claim about the text: a ready document with nothing extracted
        # is the silent empty document the ticket forbids, refused by the database
        # rather than by the branch that writes it.
        CheckConstraint(
            "status <> 'ready' OR extracted_chars > 0", name="ck_documents_ready_has_text"
        ),
        CheckConstraint(
            "status <> 'failed' OR length(btrim(coalesce(failure_reason, ''))) > 0",
            name="ck_documents_failed_has_reason",
        ),
        # The duplicate rule for personal uploads, in the database. Scoped to the
        # owner because that is what the rule is: "the same file twice" means *you*
        # already have it. Company documents are excluded — several departments may
        # legitimately hold the same policy PDF, and their owner is NULL.
        Index(
            "uq_documents_owner_content",
            "owner_employee_id",
            "content_sha256",
            unique=True,
            postgresql_where=text("owner_employee_id IS NOT NULL"),
        ),
        Index("ix_documents_content_sha256", "content_sha256"),
        # The job's query: what is waiting to be parsed.
        Index("ix_documents_status", "status", "created_at"),
        # The department list. §4.2's second clause filters on it for every caller.
        Index("ix_documents_department", "department_id", "clearance_level"),
        Index("ix_documents_owner", "owner_employee_id"),
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    #: The parent/child split of D-Q37, as a self-reference. Ticket 31 writes one row
    #: per structural block and leaves this NULL; ticket 32's retrieval work is what
    #: will write parents and point children at them, which is why the column exists
    #: now — a self-referencing foreign key is a migration either way, and doing it
    #: before there are rows is doing it once.
    parent_chunk_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="CASCADE"), nullable=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    page_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    heading_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Created, indexed, and never written by this ticket. §10.3: 1536 dimensions with
    #: `vector_cosine_ops`; a pgvector column's dimension is part of the schema, so it
    #: exists before the first embedding does or the first embedding needs a migration.
    embedding: Mapped[list | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=True)
    #: Which model produced `embedding`, or NULL when there is none. Written with the
    #: vector and never separately (migration 0022): vectors from two models are not
    #: comparable, so this column is what makes a re-embed a query rather than a guess
    #: about which rows are stale.
    embedding_model: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Which split produced this row. A re-index after the split changes is a query
    #: rather than a guess.
    chunking_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    # `search_vector` is deliberately **not** declared here. It is a generated column
    # (migration 0022) holding `to_tsvector('spanish', coalesce(heading_path,'') ||
    # ' ' || content)`, nothing in the application writes it, and anything that reads
    # it — the retrieval query and the tests that check the two halves agree — reads it
    # in SQL. A second copy of that expression in Python would be a second place for
    # the Spanish configuration to be wrong.
    __table_args__ = (
        CheckConstraint("chunk_index >= 0", name="ck_document_chunks_index"),
        CheckConstraint("token_count > 0", name="ck_document_chunks_tokens"),
        CheckConstraint(
            "page_from IS NULL OR page_to IS NULL OR page_to >= page_from",
            name="ck_document_chunks_pages",
        ),
        # The idempotence guarantee. A re-run that failed to delete first cannot
        # insert a second row for the same position; the database refuses it.
        UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_position"),
        # Migration 0022's two indexes are deliberately not declared here either, and
        # for the same reason: they are a GIN index on the generated tsvector and a
        # partial index on `embedding IS NULL`, and neither expression can be written
        # in Python without restating the SQL the migration owns.
    )


__all__ = ["Document", "DocumentChunk"]
