"""PostgreSQL implementation of the document repository.

**This file is where §4.2 becomes SQL**, and it is the only place it does. The kernel
states the rule as four clauses over a `Resource`; `filter_for` turns those into a
`FilterSpec`; this module renders the spec as a `WHERE` clause. Three things follow,
and each is a deliberate refusal to take a shortcut:

* **No method here takes a `Principal`.** A repository that re-derived departments and
  clearance from a principal would be the second implementation of the rule — the
  exact defect `docs/architecture/codebase-design.md` §3 rejects a separate "document
  access" module for. The spec is data, produced by the kernel and consumed here.

* **`_visible` is the single translation.** Every read that answers a request goes
  through it, so a new read cannot be written without a filter by accident: there is
  no other way to build the statement. The four clauses are written in the order the
  design lists them and with the same connectives, because a reader comparing this
  against §4.2 should find the same sentences.

* **The duplicate lookup is scoped by the same clause as the list.** The unique index
  in the migration keys on the owner; this query keys on what the *caller* can see.
  The two agree for personal uploads, which is what that index is for, and the query
  is the one that also covers company documents — where the same PDF may legitimately
  exist twice because two departments filed it.

`allow_all` never holds for `ResourceKind.DOCUMENT` — the kernel says so and explains
why — so this module treats it as the bug it would be rather than honouring it. A
permissive spec that arrived here would turn every read into "every row", and every
row includes other people's private uploads.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, insert, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access.kernel import FilterSpec
from app.domain.document.models import (
    DEPARTMENT_VISIBILITY,
    ChunkInput,
    Document,
    DocumentChunk,
    DocumentMetadata,
    DocumentPage,
    DocumentStatus,
)
from app.models.document import Document as DocumentRow
from app.models.document import DocumentChunk as ChunkRow
from app.models.employee import Employee as EmployeeRow
from app.models.org import Department as DepartmentRow


def _to_document(row: DocumentRow) -> Document:
    return Document(
        id=row.id,
        title=row.title,
        owner_employee_id=row.owner_employee_id,
        department_id=row.department_id,
        clearance_level=row.clearance_level,
        visibility=row.visibility,
        is_company_kb=row.is_company_kb,
        category=row.category,
        tags=tuple(row.tags or ()),
        language=row.language,
        status=DocumentStatus(row.status),
        storage_path=row.storage_path,
        content_sha256=row.content_sha256,
        filename=row.filename,
        media_type=row.media_type,
        file_size=row.file_size,
        extracted_chars=row.extracted_chars,
        page_count=row.page_count,
        chunk_count=row.chunk_count,
        failure_reason=row.failure_reason,
        uploaded_by_employee_id=row.uploaded_by_employee_id,
        parsed_at=row.parsed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_chunk(row: ChunkRow) -> DocumentChunk:
    return DocumentChunk(
        id=row.id,
        document_id=row.document_id,
        chunk_index=row.chunk_index,
        content=row.content,
        token_count=row.token_count,
        page_from=row.page_from,
        page_to=row.page_to,
        heading_path=row.heading_path,
        created_at=row.created_at,
        parent_chunk_id=row.parent_chunk_id,
        embedding_model=row.embedding_model,
    )


def _vector_literal(vector: list[float]) -> str:
    """A pgvector literal, at the precision the column stores.

    `%g`'s seven significant digits is what `vector` keeps anyway (single precision),
    so a round trip through this string cannot lose a value the column would have
    held, and a full `repr(float)` would send twice the bytes for the same row.
    """
    return "[" + ",".join(f"{value:.7g}" for value in vector) + "]"


def _visible(spec: FilterSpec):
    """§4.2, as a `WHERE` clause.

    Clause by clause, in the design's order:

    1. the caller's own personal document, whatever its classification — gated on
       `NOT is_company_kb`, so the ownership test is a statement about personal
       documents and a company document reaches a caller through clause 2 (with its
       department and its ceiling) and not by naming an owner;
    2. a company knowledge-base document within the ceiling *and* in a department the
       caller reaches;
    3. a colleague's personal document published to the department, within the ceiling —
       rendered only when the spec carries `personal_documents_via_department`, which
       the document list sets and the retrieval path clears (ticket 36);
    4. the exception roles, for company documents, within the ceiling.

    **Clause 3 is not `document_permissions`.** The design's §3.6 has a table for
    person-by-person grants; this schema has no such table, and the share this ticket
    asks for is the two-value `visibility` column the design already gives `documents`
    (`private`/`department`/`company`). So the clause reads `visibility = 'department'`
    and the unchanged `clearance_level` ceiling — 显式共享不能突破密级上限 — rather than a
    lookup into a table nothing writes. The earlier `false` placeholder is therefore
    replaced by the rule, not deferred.

    The department a personal document is published to is carried in
    `documents.department_id`, the same column the company clause uses, so "shared with
    my department" is one predicate over columns that already exist.
    """
    if spec.allow_all:  # pragma: no cover - `filter_for` never produces this
        raise ValueError(
            "a document filter with allow_all would reach every private upload; "
            "the kernel does not produce one and this module will not honour it"
        )

    clauses = []
    if spec.own_employee_id is not None:
        clauses.append(
            and_(
                DocumentRow.is_company_kb.is_(False),
                DocumentRow.owner_employee_id == spec.own_employee_id,
            )
        )

    company = DocumentRow.is_company_kb.is_(True)
    within_ceiling = DocumentRow.clearance_level.in_(spec.clearance_levels)
    if spec.include_company_kb:
        clauses.append(
            and_(company, within_ceiling, DocumentRow.department_id.in_(spec.department_ids))
        )
    if spec.personal_documents_via_department:
        clauses.append(
            and_(
                DocumentRow.is_company_kb.is_(False),
                DocumentRow.visibility == DEPARTMENT_VISIBILITY,
                within_ceiling,
                DocumentRow.department_id.in_(spec.department_ids),
            )
        )
    if spec.company_kb_cross_department:
        clauses.append(and_(company, within_ceiling))
    if not clauses:  # pragma: no cover - a spec with no reach is still a valid refusal
        return DocumentRow.id.is_(None)
    return or_(*clauses)


class PostgresDocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- reads that answer a request ---------------------------------------

    async def page_for(self, spec: FilterSpec, *, limit: int, offset: int) -> DocumentPage:
        statement = select(DocumentRow).where(_visible(spec))
        total = await self._session.scalar(
            select(func.count()).select_from(statement.subquery())
        )
        rows = await self._session.scalars(
            statement.order_by(DocumentRow.created_at.desc(), DocumentRow.id)
            .limit(limit)
            .offset(offset)
        )
        return DocumentPage(
            items=[_to_document(row) for row in rows],
            total=int(total or 0),
            limit=limit,
            offset=offset,
        )

    async def get_for(self, spec: FilterSpec, document_id: UUID) -> Document | None:
        row = await self._session.scalar(
            select(DocumentRow).where(DocumentRow.id == document_id, _visible(spec))
        )
        return _to_document(row) if row is not None else None

    async def by_hash_for(self, spec: FilterSpec, content_sha256: str) -> Document | None:
        row = await self._session.scalar(
            select(DocumentRow)
            .where(DocumentRow.content_sha256 == content_sha256, _visible(spec))
            # Oldest first, so a file uploaded twice answers with the copy that has
            # been there longest rather than with whichever row the planner returns.
            .order_by(DocumentRow.created_at)
            .limit(1)
        )
        return _to_document(row) if row is not None else None

    # --- reads and writes the job uses --------------------------------------

    async def get(self, document_id: UUID) -> Document | None:
        row = await self._session.scalar(
            select(DocumentRow).where(DocumentRow.id == document_id)
        )
        return _to_document(row) if row is not None else None

    async def pending_ids(self, *, limit: int) -> list[UUID]:
        rows = await self._session.scalars(
            select(DocumentRow.id)
            .where(DocumentRow.status == DocumentStatus.PROCESSING.value)
            .order_by(DocumentRow.created_at, DocumentRow.id)
            .limit(limit)
        )
        return list(rows)

    async def chunks(self, document_id: UUID, *, limit: int = 200) -> list[DocumentChunk]:
        rows = await self._session.scalars(
            select(ChunkRow)
            .where(ChunkRow.document_id == document_id)
            .order_by(ChunkRow.chunk_index)
            .limit(limit)
        )
        return [_to_chunk(row) for row in rows]

    async def chunks_missing_embedding(self, *, limit: int) -> list[UUID]:
        """Documents with at least one *child* that has no vector: the re-embed worklist.

        **The predicate is `parent_chunk_id IS NOT NULL AND embedding IS NULL`, and the
        first half is what makes the list terminate.** A parent is context and is
        deliberately never embedded, so a query that asked only for a NULL embedding
        would name every document that has a parent row — which is every document the
        split produced more than one section for. The job would then re-embed the corpus
        on every pass, for ever, and each pass would report work done.

        `EXISTS` rather than a join with `DISTINCT`, so the plan is a semi-join over the
        partial index and a document with fifty missing vectors is read once. A document
        whose embedding call failed, one chunked while the provider was `none`, and one
        chunked before the model was configured are the same row shape here, and they
        want the same remedy.
        """
        rows = await self._session.scalars(
            select(DocumentRow.id)
            .where(
                select(ChunkRow.id)
                .where(
                    ChunkRow.document_id == DocumentRow.id,
                    ChunkRow.parent_chunk_id.is_not(None),
                    ChunkRow.embedding.is_(None),
                )
                .exists()
            )
            .order_by(DocumentRow.created_at, DocumentRow.id)
            .limit(limit)
        )
        return list(rows)

    async def chunk_texts(self, document_id: UUID) -> list[tuple[int, str]]:
        rows = await self._session.execute(
            select(ChunkRow.chunk_index, ChunkRow.content)
            .where(ChunkRow.document_id == document_id)
            .order_by(ChunkRow.chunk_index)
        )
        return [(int(index), content) for index, content in rows]

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
        """Write the row in `processing`, and answer with it.

        **A Core INSERT, and the id generated here.** Both are load-bearing, and for the
        same reason: any statement that *returns* the written row makes PostgreSQL apply
        the table's SELECT policy to it — the ORM's own flush emits
        `INSERT ... RETURNING id` for a client-side primary key, and `RETURNING` is a
        read. The read rule admits a caller's own documents and their department's, so a
        company document — no owner, and filed into a department its author may not
        belong to — could never be inserted, and the failure reads "new row violates
        row-level security policy", which points at the insert rule rather than at the
        `RETURNING`. `test_documents.py` pins the difference directly.

        Assembling the value object from what was written is therefore the honest shape:
        every field is known here, and the two the database fills in (`created_at`,
        `updated_at`) are its clock, which the next read gets from the row itself.
        """
        document_id = uuid4()
        now = datetime.now(UTC)
        values = {
            "id": document_id,
            "title": title,
            "owner_employee_id": owner_employee_id,
            "department_id": metadata.department_id,
            "clearance_level": metadata.clearance_level,
            "visibility": metadata.effective_visibility(),
            "is_company_kb": metadata.is_company_kb,
            "category": metadata.category,
            "tags": list(metadata.tags),
            "language": metadata.language,
            "status": DocumentStatus.PROCESSING.value,
            "storage_path": storage_path,
            "content_sha256": content_sha256,
            "filename": filename,
            "media_type": media_type,
            "file_size": file_size,
            "extracted_chars": 0,
            "chunk_count": 0,
            "uploaded_by_employee_id": uploaded_by_employee_id,
        }
        await self._session.execute(insert(DocumentRow).values(**values))
        await self._session.flush()
        return Document(
            id=document_id,
            title=title,
            owner_employee_id=owner_employee_id,
            department_id=metadata.department_id,
            clearance_level=metadata.clearance_level,
            visibility=metadata.effective_visibility(),
            is_company_kb=metadata.is_company_kb,
            category=metadata.category,
            tags=tuple(metadata.tags),
            language=metadata.language,
            status=DocumentStatus.PROCESSING,
            storage_path=storage_path,
            content_sha256=content_sha256,
            filename=filename,
            media_type=media_type,
            file_size=file_size,
            extracted_chars=0,
            page_count=None,
            chunk_count=0,
            failure_reason=None,
            uploaded_by_employee_id=uploaded_by_employee_id,
            parsed_at=None,
            created_at=now,
            updated_at=now,
        )

    async def set_status(
        self,
        document_id: UUID,
        status: DocumentStatus,
        *,
        failure_reason: str | None = None,
    ) -> Document:
        await self._session.execute(
            update(DocumentRow)
            .where(DocumentRow.id == document_id)
            .values(
                status=status.value,
                failure_reason=failure_reason if status is DocumentStatus.FAILED else None,
                updated_at=datetime.now(UTC),
            )
        )
        await self._session.flush()
        return await self._require(document_id)

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
        written = await self.replace_chunks(document_id, chunks)
        if written != chunk_count:  # pragma: no cover - defensive; the count is derived
            raise LookupError(
                f"document {document_id}: {written} chunks written for a stated {chunk_count}"
            )
        await self._session.execute(
            update(DocumentRow)
            .where(DocumentRow.id == document_id)
            .values(
                status=status.value,
                extracted_chars=extracted_chars,
                page_count=page_count,
                chunk_count=chunk_count,
                failure_reason=failure_reason if status is DocumentStatus.FAILED else None,
                parsed_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await self._session.flush()
        return await self._require(document_id)

    async def replace_chunks(self, document_id: UUID, chunks: list[ChunkInput]) -> int:
        """Delete this document's chunks, then write these, links included.

        The delete happens even when the list is empty, which is the case a failed
        parse produces: a document that had chunks and now has none must lose them, or
        a retry that fails would leave the previous run's text retrievable beside a
        `failed` status.

        **The parent links are written in a second pass, after the ids exist.** Rows
        are inserted in `chunk_index` order with `parent_chunk_id` NULL, the flush
        assigns each one its uuid, and then every child is pointed at the row its
        `parent_index` names. The link survives a rewrite because it is rebuilt from
        the split every time — the rows the previous version's links referred to are
        gone, deleted in the statement above, so nothing can dangle.
        """
        await self._session.execute(
            delete(ChunkRow).where(ChunkRow.document_id == document_id)
        )
        written: list[tuple[ChunkRow, int | None]] = []
        for chunk in sorted(chunks, key=lambda item: item.chunk_index):
            row = ChunkRow(
                id=uuid4(),
                document_id=document_id,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                token_count=chunk.token_count,
                page_from=chunk.page_from,
                page_to=chunk.page_to,
                heading_path=chunk.heading_path,
                chunking_version=chunk.chunking_version,
                embedding=chunk.embedding,
                embedding_model=chunk.embedding_model,
            )
            self._session.add(row)
            written.append((row, chunk.parent_index))
        # The flush is what gives every row an id; until it runs there is nothing for a
        # child to point at.
        await self._session.flush()

        place = {row.chunk_index: row.id for row, _ in written}
        linked = 0
        for row, parent_index in written:
            if parent_index is None:
                continue
            row.parent_chunk_id = place[parent_index]
            linked += 1
        if linked:
            await self._session.flush()
        return len(written)

    async def set_embeddings(
        self,
        document_id: UUID,
        vectors: dict[int, list[float]],
        model: str,
    ) -> int:
        """Write vectors onto existing rows, by `chunk_index`.

        One `UPDATE ... FROM (VALUES ...)`, so a document's vectors land in one
        statement: a loop of updates over a ten-thousand-chunk workbook would be ten
        thousand round trips, and the write is the same either way.

        The model is written with the vector and never apart from it. A row whose
        `embedding_model` says one thing while its vector came from another is a row
        retrieval would happily compare against vectors it is not commensurate with —
        the schema cannot check that, so the write is the only place it can be true.
        """
        if not vectors:
            return 0
        rows = sorted(vectors.items())
        values = ", ".join(
            f"({index}, CAST(:v{position} AS vector))" for position, (index, _) in enumerate(rows)
        )
        parameters: dict[str, object] = {
            f"v{position}": _vector_literal(vector) for position, (_, vector) in enumerate(rows)
        }
        parameters["document_id"] = document_id
        parameters["model"] = model
        result = await self._session.execute(
            text(
                f"""
                UPDATE document_chunks AS c
                   SET embedding = v.embedding, embedding_model = :model
                  FROM (VALUES {values}) AS v(chunk_index, embedding)
                 WHERE c.document_id = :document_id
                   AND c.chunk_index = v.chunk_index
                """
            ),
            parameters,
        )
        await self._session.flush()
        return int(result.rowcount or 0)

    # --- plumbing -----------------------------------------------------------

    async def employee_exists(self, employee_id: UUID) -> bool:
        return bool(
            await self._session.scalar(
                select(func.count()).select_from(EmployeeRow).where(EmployeeRow.id == employee_id)
            )
        )

    async def department_exists(self, department_id: UUID) -> bool:
        return bool(
            await self._session.scalar(
                select(func.count())
                .select_from(DepartmentRow)
                .where(DepartmentRow.id == department_id)
            )
        )

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()

    async def _require(self, document_id: UUID) -> Document:
        document = await self.get(document_id)
        if document is None:  # pragma: no cover - the service reads the row first
            raise LookupError(f"document {document_id} disappeared between two reads")
        return document


__all__ = ["PostgresDocumentRepository"]
