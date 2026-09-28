"""Documents, their chunks, and the row-level policy ticket 13 wrote for them.

Revision ID: 0021
Revises: 0020
Created: 2026-10-04

**Chain position:** 0020 (`20261003_1000_overtime.py`, ticket 26) → 0021 (this, ticket
31). The directory was read immediately before this file was written — `alembic heads`
reported `0020` and nothing else — because the ids have collided repeatedly in this
project.

Two tables (DESIGN §3.6) and **five row-level policies**, which is the part worth
reading — four of them were found by something failing, and each is recorded here
because a row-level rule that is missing looks exactly like one that is working:

* **`document_visibility_predicate` is attached, not re-implemented.** Ticket 13
  created that function *for this migration* — its docstring says so in as many words
  — with the three columns a document's row-level rule needs: the owner, the
  department and the clearance. Here it becomes the `FOR SELECT` policy on
  `documents`, and the same predicate is used by `document_chunks` through a lookup of
  its document:
  `document_visibility_predicate(d.owner_employee_id, d.department_id,
  d.clearance_level)`. Neither policy spells the rule out a second time, so the two
  layers cannot drift: the function is the statement of what §4.2 looks like as a row
  predicate, and every table that holds documents is governed by it.

* **`documents_insert`, and without it every upload failed.** With row-level security
  enabled and only a `FOR SELECT` policy, PostgreSQL applies that policy's `USING`
  clause to the new row of an INSERT — so a request was refused with `new row violates
  row-level security policy for table documents` whatever the caller held. The insert
  rule is the read rule stated forwards: yours, or a company document within your
  clearance and either in a department you reach or filed by a role whose job is the
  knowledge base. That last branch is administration's, and it is the one thing the
  read rule does not need: `admin` configures the organisation rather than belonging to
  a department, so a rule written only as "a department you are in" refused every
  company document an administrator filed.

  **`INSERT ... RETURNING` is refused by the same policy, and that is worth knowing.**
  The returned columns are a *read* of the new row, so the SELECT policy applies to
  them — which means a company document could not be inserted by any statement that
  asked for its id back. `repositories/document.py` therefore writes with a Core INSERT
  on a client-generated uuid; `test_documents.py` pins the difference directly.

* **`documents_system`, the pipeline's own read.** The parsing job runs as the same
  runtime role as a request and has no user context, so with only the visibility
  policies it saw *no documents at all* — `pending_ids` returned the empty list and the
  pipeline processed nothing while reporting success. That is the backstop doing its job
  and the job being wrong about who it is. `app.current_system = 'true'` is how it says
  otherwise; it is published only by `jobs/parse_documents`, and `apply_rls_context`
  never writes it, so no request can obtain it by accident. The alternative was to run
  the job on the owner connection — the privilege the whole design keeps out of
  processes that touch request data — and a named flag is smaller, and visible in
  `pg_policies`, which is where an auditor looks.

  It is `FOR SELECT` and not `FOR ALL` on purpose: an `ALL` policy carries a `WITH
  CHECK` that every write is measured against, so a permissive `ALL` whose check is
  false silently refuses *every* insert and update. The writes the pipeline does are
  stated where they belong, in the two policies either side of this one.

* **`documents_write`, and without it `reprocess` updated nothing.** A request that
  moves a status is a user's act, not the pipeline's, so `documents_system` cannot be
  what admits it. `documents_write` is the update rule read forwards — you may move a
  document you could read — and its absence is the quietest failure in this file:
  `UPDATE` affected zero rows *and the endpoint answered 200 with the old status*,
  because a permissive policy whose `WITH CHECK` fails makes the whole statement a
  no-op rather than an error. Columns are not the policy's business; §4.2 has no
  per-column clauses, and the field-level decisions are the kernel's, checked in the
  service before anything is written.

* **`document_chunks_access`, one `FOR ALL` policy on the chunks.** Its `USING` clause
  is §4.2 through the chunk's document — so a retrieval query cannot read a chunk of a
  document the caller cannot open, which is the half that matters most — and its `WITH
  CHECK` admits the system alone, because nothing but the pipeline writes a chunk. One
  policy rather than three because there is exactly one writer and one read rule, and a
  second permissive policy here would have to agree with this one about both.

* **The policies are narrower than §4.2 and never wider.** They express clause 1
  (ownership, whatever the classification) and clause 2 (the company knowledge base,
  within the ceiling and in a department the caller reaches), plus the write rules
  above. They cannot express clause 3, because a predicate cannot see
  `document_permissions`, or clause 4's role set, because roles are the kernel's
  question — except in the insert branch, where a role has to be named or administration
  could not file anything at all. A backstop *under* an application rule is allowed to
  refuse more than the application does and must never refuse less, which is the
  direction this errs in, and `test_documents.py` asserts both directions.

* **Administrators and the exception roles get no shortcut on the read.** Unlike
  `employee_private`, nothing in `documents_visibility` reads `app.current_roles` or
  `app.is_privileged`. An HR member reaching another department's company document does
  so through the kernel's fourth clause and through the *application's* filter; the
  database is the line under that, not a second copy of it. The consequence is
  deliberate and asserted: a query written with no filter returns the caller's own
  documents and their department's, and nothing else.

* **`document_chunks.embedding` is created and left NULL.** §10.3 fixes the dimension
  at 1536 with an HNSW index on `vector_cosine_ops`, and a pgvector column's dimension
  is part of the schema. Ticket 32 fills the column; this migration is where the
  decision is spent, because changing it afterwards means re-embedding every row.

* **`document_chunks` keeps every privilege, including DELETE, and that is a decision
  rather than an oversight.** The tempting REVOKE — a chunk is what an answer is quoted
  from, so make it append-only — would break the one operation this ticket's retry
  requirement rests on: `replace_chunks` deletes a document's chunks before writing the
  new split, and a runtime role that cannot delete its own document's chunks cannot
  re-parse anything. The idempotence is enforced underneath by the unique
  `(document_id, chunk_index)`, which is what stops an append from doubling a document.

* **`documents` keeps UPDATE and loses DELETE.** A document's status moves and its text
  length and failure reason are written, so UPDATE stays. Removing a document is not a
  thing this ticket does, and "the original file is kept" is the design's own
  requirement (保留原始文件): a runtime role that can DELETE a document is a role that
  can orphan the bytes behind it. The privilege is asserted, for both tables, in
  `test_documents.py`.

No `GRANT` statement: migration 0007 set default privileges, so tables added later are
reachable by the runtime role without one. That claim is asserted, for these two
tables, in `tests/test_documents.py`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "eam_app"

#: Kept as a literal, like migration 0001's: a migration describes the schema it
#: applied even if the application constant changes later, and
#: `tests/test_documents.py` asserts the two agree.
VECTOR_DIMENSIONS = 1536

#: The policy names, written out so `downgrade` drops exactly what `upgrade` created.
DOCUMENT_POLICY = "documents_visibility"
DOCUMENT_INSERT_POLICY = "documents_insert"
DOCUMENT_SYSTEM_POLICY = "documents_system"
DOCUMENT_WRITE_POLICY = "documents_write"
CHUNK_POLICY = "document_chunks_access"

#: The setting the parsing job publishes to say it is the system rather than a user.
#: Named here as well as in `jobs/parse_documents.py` so a reader of either file finds
#: the other.
SYSTEM_SETTING = "app.current_system"

#: The predicate both system policies use. A string comparison because the setting is
#: text; `app_setting` folds a written-then-abandoned empty string into NULL, so an
#: absent flag is NULL, NULL is not `'true'`, and the row is refused.
SYSTEM = f"app_setting('{SYSTEM_SETTING}') = 'true'"

#: The roles that file a *company* document into a department that is not their own:
#: `DOCUMENT_CROSS_DEPARTMENT_ROLES` (§4.2's fourth clause) and the two that hold
#: `document.manage`. Administration is why this exists at all — it configures the
#: organisation rather than belonging to a department, so an insert rule written as
#: "your own department" would refuse every company document an administrator files.
KNOWLEDGE_BASE_ROLES = "ARRAY['admin', 'hr', 'compliance']"


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        # NULL exactly when the document is the company's; see the CHECK below.
        sa.Column("owner_employee_id", sa.UUID(), nullable=True),
        sa.Column("department_id", sa.UUID(), nullable=True),
        sa.Column("clearance_level", sa.String(length=6), nullable=False),
        sa.Column("visibility", sa.String(length=12), nullable=False),
        sa.Column("is_company_kb", sa.Boolean(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column(
            "tags",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("language", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        # The stored original. A key under the storage root, never an absolute path.
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("extracted_chars", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uploaded_by_employee_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "clearance_level IN ('low', 'medium', 'high')", name="ck_documents_clearance"
        ),
        sa.CheckConstraint(
            "visibility IN ('private', 'department', 'company')", name="ck_documents_visibility"
        ),
        # The design's own annotation, as an equivalence: an owner without the company
        # flag is a personal upload, and the company flag without an owner is the
        # company's. A row that disagrees with itself is refused here.
        sa.CheckConstraint(
            "(owner_employee_id IS NULL) = is_company_kb", name="ck_documents_company_owner"
        ),
        # §4.2 reaches a company document *through its department*. One without a
        # department has no owner and no department, so no clause admits it and nobody
        # but an owner who does not exist could read it. Refused rather than stored.
        sa.CheckConstraint(
            "NOT is_company_kb OR department_id IS NOT NULL",
            name="ck_documents_company_department",
        ),
        sa.CheckConstraint(
            "status IN ('processing', 'ready', 'failed', 'archived')", name="ck_documents_status"
        ),
        sa.CheckConstraint("file_size > 0", name="ck_documents_file_size"),
        sa.CheckConstraint("length(content_sha256) = 64", name="ck_documents_sha256"),
        sa.CheckConstraint("extracted_chars >= 0", name="ck_documents_extracted"),
        sa.CheckConstraint("chunk_count >= 0", name="ck_documents_chunks"),
        # "ready" is a claim about the text. The ticket forbids a silent empty
        # document, and this is where that is a database rule rather than a branch
        # somebody has to remember: a scanned file that set `ready` with zero
        # characters cannot be stored at all.
        sa.CheckConstraint(
            "status <> 'ready' OR extracted_chars > 0", name="ck_documents_ready_has_text"
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR length(btrim(coalesce(failure_reason, ''))) > 0",
            name="ck_documents_failed_has_reason",
        ),
        sa.ForeignKeyConstraint(
            ["owner_employee_id"], ["employees.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["uploaded_by_employee_id"], ["employees.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # "The same file twice", as the database sees it: one row per owner per content.
    # Partial, because a company document's owner is NULL and several departments may
    # legitimately hold the same policy PDF.
    op.create_index(
        "uq_documents_owner_content",
        "documents",
        ["owner_employee_id", "content_sha256"],
        unique=True,
        postgresql_where=sa.text("owner_employee_id IS NOT NULL"),
    )
    op.create_index("ix_documents_content_sha256", "documents", ["content_sha256"])
    # The job's query: what is waiting to be parsed, oldest first.
    op.create_index("ix_documents_status", "documents", ["status", "created_at"])
    # §4.2's second clause, for every caller that lists documents.
    op.create_index(
        "ix_documents_department", "documents", ["department_id", "clearance_level"]
    )
    op.create_index("ix_documents_owner", "documents", ["owner_employee_id"])

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        # The parent/child split of D-Q37. NULL in this ticket; ticket 32's retrieval
        # work writes the parents. The self-reference exists now because adding a
        # foreign key before there are rows is doing it once.
        sa.Column("parent_chunk_id", sa.UUID(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("page_from", sa.Integer(), nullable=True),
        sa.Column("page_to", sa.Integer(), nullable=True),
        sa.Column("heading_path", sa.Text(), nullable=True),
        # Created and left NULL. §10.3: 1536 dimensions, HNSW, cosine. Ticket 32 fills
        # it; the dimension is spent here because pgvector cannot change it in place.
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("chunking_version", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("chunk_index >= 0", name="ck_document_chunks_index"),
        sa.CheckConstraint("token_count > 0", name="ck_document_chunks_tokens"),
        sa.CheckConstraint(
            "page_from IS NULL OR page_to IS NULL OR page_to >= page_from",
            name="ck_document_chunks_pages",
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_chunk_id"], ["document_chunks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Idempotence, as a constraint: a re-run that failed to delete first cannot
        # write a second row for the same position.
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_position"),
    )
    # The pgvector column, its index and its operator class. `ALTER TABLE` rather than
    # a `sa.Column` above because the type has no SQLAlchemy-native spelling that
    # `create_table` renders: pgvector registers `vector` as a column type, and the
    # DDL is written where the dimension is visible next to the literal that fixes it.
    #
    # The `USING` clause is mandatory, not tidiness: PostgreSQL refuses a `text` →
    # `vector` cast without it, so the column is created with a placeholder type and
    # converted here. On the empty table this migration creates the conversion is a
    # catalogue change; the clause is written out anyway, because a migration that only
    # works on an empty table is a migration that will be run against a full one.
    op.execute(
        f"ALTER TABLE document_chunks ALTER COLUMN embedding TYPE vector({VECTOR_DIMENSIONS}) "
        "USING embedding::vector"
    )
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding_hnsw ON document_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX ix_document_chunks_document ON document_chunks (document_id, chunk_index)"
    )

    _attach_document_policies()


def _attach_document_policies() -> None:
    """Ticket 13's predicate, attached to both tables.

    Not written out again here. `document_visibility_predicate(owner, department,
    clearance)` is the rule §4.2 collapses to when it is expressed as a row predicate,
    and its own migration says it exists so the tables added later would call it. A
    second copy of the condition in this file is the copy that drifts.

    `document_chunks` carries none of the three columns, so its policy asks its
    document. The lookup inside a policy function is what the design's §4.3 sketch
    does too (`JOIN documents d ON d.id = c.document_id`), and the performance of it
    is the measurement `docs/DESIGN.md` §10.2 already records.

    **Three policies, and the third one was missing from the first draft.** With RLS
    enabled and only `FOR SELECT` policies, PostgreSQL applies the select policy to the
    new row of an INSERT, so every upload failed with "new row violates row-level
    security policy" — a green-looking migration and an endpoint that could not write a
    single document. `documents_insert` is the same rule read forwards: write what you
    could read. `document_chunks` needs no insert policy because the chunks are written
    through the *pipeline*, whose connection has already published the uploader's
    context and whose rows name a document that passed the same test.
    """
    op.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {DOCUMENT_POLICY} ON documents
        FOR SELECT
        USING (
            document_visibility_predicate(owner_employee_id, department_id, clearance_level)
            OR {SYSTEM}
        )
        """
    )
    # **An INSERT policy as well, and it is not optional.** With row-level security
    # enabled and only a `FOR SELECT` policy, PostgreSQL applies the select policy's
    # `USING` clause to the *new* row of an INSERT — so every upload was refused with
    # "new row violates row-level security policy for table documents", whatever the
    # caller held. The insert rule is the visibility rule read forwards: you may write
    # a document you could then read, which is exactly what the application checks
    # before it gets here (own upload, or a company document in a department you are
    # in). It is written out rather than reusing the helper because `WITH CHECK` runs
    # before the row exists and the helper is about reading one — the two say the same
    # thing from opposite ends and neither can be derived from the other mechanically.
    #
    # The role branch is administration's, and it is the one thing the read rule does
    # not need: `admin` configures the organisation rather than belonging to a
    # department, so a rule written only as "a department you are in" refused every
    # company document an administrator filed.
    op.execute(
        f"""
        CREATE POLICY {DOCUMENT_INSERT_POLICY} ON documents
        FOR INSERT
        WITH CHECK (
            owner_employee_id = app_setting('app.current_employee_id')::uuid
            OR (
                is_company_kb
                AND clearance_level = ANY (app_setting_array('app.clearance_levels'))
                AND (
                    department_id = ANY (app_setting_array('app.department_ids')::uuid[])
                    OR app_setting_array('app.current_roles') && {KNOWLEDGE_BASE_ROLES}
                )
            )
            OR {SYSTEM}
        )
        """
    )
    # The pipeline's own reach. `SELECT`, not `ALL`: an `ALL` policy carries a
    # `WITH CHECK` that every write is measured against, so a permissive `ALL` whose
    # check is false silently refuses *every* insert and update — the row-level
    # equivalent of an `OR` that someone read as an `AND`. The writes the pipeline does
    # are stated where they belong: `documents_insert` for the upload, `documents_write`
    # for the status move, and both of those name the system in their own checks.
    op.execute(
        f"""
        CREATE POLICY {DOCUMENT_SYSTEM_POLICY} ON documents
        FOR SELECT
        USING ({SYSTEM})
        """
    )
    # **A fourth policy, and the reason it exists is subtle enough to write down.** A
    # permissive policy whose command is `ALL` and whose `WITH CHECK` fails does not
    # merely fail to admit the row: it makes the *whole* UPDATE affect zero rows, even
    # when another policy's `USING` clause admits them. So with only the three above, a
    # request that moved a document's status — `reprocess`, which is a user's act, not
    # the pipeline's — silently updated nothing and answered 200 with the old status.
    # `documents_write` is the update rule read forwards: you may move a document you
    # could read. Columns are not the policy's business; §4.2 has no per-column clauses,
    # and the field-level decisions (who may classify, who may archive) are the
    # kernel's, checked in the service before anything is written.
    #
    # `USING` is the system's alone rather than the predicate, and that asymmetry is
    # deliberate: a row you may not read is a row you may not update, and the *new* row
    # is the one the check is about. Both halves admit the system, which is the only
    # caller that ever moves a document this way.
    op.execute(
        f"""
        CREATE POLICY {DOCUMENT_WRITE_POLICY} ON documents
        FOR UPDATE
        USING ({SYSTEM} OR document_visibility_predicate(
            owner_employee_id, department_id, clearance_level
        ))
        WITH CHECK (
            {SYSTEM}
            OR document_visibility_predicate(
                owner_employee_id, department_id, clearance_level
            )
        )
        """
    )

    op.execute("ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY")
    # One policy, `FOR ALL`, saying two things at once: reading a chunk follows its
    # document's visibility (§4.2 through the join the design's §4.3 sketch also
    # makes), and writing one is the system's alone. `USING` governs SELECT, UPDATE
    # and DELETE; `WITH CHECK` governs INSERT and UPDATE. A request therefore reads the
    # chunks of documents it may open and writes none — which is what makes a
    # retrieval query safe even if it forgets its filter.
    #
    # `USING` is the disjunction because a chunk is reachable two ways; `WITH CHECK` is
    # the system alone because nothing but the pipeline writes chunks. One policy rather
    # than three because there is exactly one writer and one read rule, and a second
    # permissive policy on this table would have to agree with this one about both.
    op.execute(
        f"""
        CREATE POLICY {CHUNK_POLICY} ON document_chunks
        FOR ALL
        USING (
            {SYSTEM}
            OR EXISTS (
                SELECT 1
                FROM documents d
                WHERE d.id = document_chunks.document_id
                  AND document_visibility_predicate(
                      d.owner_employee_id, d.department_id, d.clearance_level
                  )
            )
        )
        WITH CHECK ({SYSTEM})
        """
    )

    # The original file and its row are kept: "保留原始文件" is the design's requirement,
    # and a runtime role that can delete a document is a role that can orphan the bytes
    # behind it. UPDATE stays — the pipeline moves `status` and writes what it found.
    op.execute(f"REVOKE DELETE ON documents FROM {APP_ROLE}")
    # Chunks are different: `replace_chunks` deletes before it inserts, which is what
    # makes a re-parse idempotent rather than duplicating. Revoking DELETE here would
    # break `reprocess` — so it is deliberately not revoked, and this comment is the
    # record that the omission was a decision.
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON document_chunks TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {CHUNK_POLICY} ON document_chunks")
    op.execute("ALTER TABLE document_chunks DISABLE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {DOCUMENT_WRITE_POLICY} ON documents")
    op.execute(f"DROP POLICY IF EXISTS {DOCUMENT_SYSTEM_POLICY} ON documents")
    op.execute(f"DROP POLICY IF EXISTS {DOCUMENT_INSERT_POLICY} ON documents")
    op.execute(f"DROP POLICY IF EXISTS {DOCUMENT_POLICY} ON documents")
    op.execute("ALTER TABLE documents DISABLE ROW LEVEL SECURITY")
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_document")
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding_hnsw")
    op.drop_table("document_chunks")
    op.drop_index("ix_documents_owner", table_name="documents")
    op.drop_index("ix_documents_department", table_name="documents")
    op.drop_index("ix_documents_status", table_name="documents")
    op.drop_index("ix_documents_content_sha256", table_name="documents")
    op.drop_index("uq_documents_owner_content", table_name="documents")
    op.drop_table("documents")
    # `document_visibility_predicate` is left in place: migration 0007 owns it, and
    # other databases on this server may still be using it.
