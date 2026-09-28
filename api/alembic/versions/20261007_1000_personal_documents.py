"""Personal documents: the visibility rule, the answer's marker, and a narrower policy.

Revision ID: 0025
Revises: 0024
Created: 2026-10-07

**Chain position:** 0024 (`20261006_1000_answers.py`, ticket 34) → 0025 (this, ticket
36). The directory *and* `alembic heads` were read immediately before this file was
written — `heads` reported `0024` and nothing else — because the ids have collided
repeatedly in this project.

Three changes, and the third is the one worth reading:

* **`documents_visibility_kind`**: a CHECK that `visibility = 'company'` exactly when
  `is_company_kb`, i.e. that a personal upload is `private` or `department` and a
  personal upload that asked to be `company` cannot be stored. §3.6's `visibility`
  column already existed; what ticket 36 adds is that two of its three values are a
  *degree of sharing a person chooses* and the third is what the kind flag derives.
  Without the constraint the two could disagree — a personal document claiming to be
  the company knowledge base is a row two readings of §4.2 answer differently, which is
  the shape of bug that survives review.

* **`rag_messages.source_notice`**: §5.2/Q29's 「以下内容来自个人文档（非公司知识库）」
  marker, nullable, stored on the answer. No default and no backfill: the marker is
  appended to an answer that quotes a personal document, so NULL is the ordinary case
  and every existing row is correctly NULL.

* **`document_visibility_predicate` gains two parameters and the documents policies are
  re-attached to it.** This is the *second line of defence*, and the first version was
  **wider than the rule it backs up** — the one direction a backstop must never err in.
  It tested `owner = me OR (clearance_ok AND department_ok)` with no notion of what kind
  of document it was looking at, so a personal upload filed into a department was
  readable at the database layer by every colleague cleared for that department, whatever
  its `visibility` said. Ticket 36 introduces `private`/`department` as a real
  distinction and this policy has to honour it, so the predicate now takes the two
  columns that decide the clause — `is_company_kb` and `visibility` — and admits:

      1. the caller's own document (ownership has no conditions — §4.2 clause 1);
      2. a company knowledge-base document, within the ceiling *and* in a department the
         caller reaches;
      3. a personal document its owner published to a department, within the ceiling and
         in a department the caller reaches.

  Clause 3 is what makes the policy agree with the application's list; it is deliberately
  the *only* widening, and it is a widening the application already performs. Everything
  else stays a refusal, and the backstop is still narrower than §4.2 — it cannot see
  roles, so the exception clause is absent, exactly as ticket 31's migration recorded.

  **The function is replaced, not overloaded, and the policy order below matters.** A
  policy that calls a function is a dependency: `DROP FUNCTION` without `CASCADE` fails
  while the policies that reference it exist, and `CASCADE` would drop the policies
  silently and leave the tables unfiltered. So the policies are dropped first, in the
  order that respects `document_chunks_access`'s reference to `documents`, then the
  function is replaced, then every policy is recreated — and `downgrade` retraces the
  same path with the three-parameter function ticket 13 wrote.

  Two of the policies are **dropped and recreated verbatim** (`documents_system` and
  `documents_write` do not call the predicate with document columns) and one is not
  touched at all: `documents_insert` predates the predicate and is left exactly as ticket
  31 wrote it, because ticket 36 changes no write rule.

No new table and no `document_permissions`. §3.6 lists one for person-by-person grants;
this ticket's sharing is the two-value `visibility` column the design already gives
`documents`, which is what the checklist asks for (仅自己 / 本部门) and what the review of
a personal document's reach can be read off. Adding an unwritten table to hold a second
way of saying the same thing would be a second rule to keep in step, which §4.3's own
checklist line rejects.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The policies that call `document_visibility_predicate` with a document's own columns,
#: written out so `upgrade` and `downgrade` drop exactly what they recreate.
DOCUMENT_POLICY = "documents_visibility"
DOCUMENT_WRITE_POLICY = "documents_write"
CHUNK_POLICY = "document_chunks_access"

#: The settings the policies read. Spelled here as migration 0021 spells them: a migration
#: describes the schema it applied, and the application constants may move afterwards.
SYSTEM_SETTING = "app.current_system"
SYSTEM = f"app_setting('{SYSTEM_SETTING}') = 'true'"
ME = "app_setting('app.current_employee_id')::uuid"
CLEARANCES = "app_setting_array('app.clearance_levels')"
DEPARTMENTS = "app_setting_array('app.department_ids')::uuid[]"

#: The rule, as a row predicate over a document's own columns. Kept as one string because
#: the policy and the function must say the same thing: the policy calls the function, so
#: this is what the function's body is built from and what a reader compares against §4.2.
PREDICATE_SQL = f"""
        CREATE OR REPLACE FUNCTION document_visibility_predicate(
            owner_employee_id uuid,
            department_id uuid,
            clearance_level text,
            is_company_kb boolean,
            visibility text
        ) RETURNS boolean AS $$
        BEGIN
            RETURN
                owner_employee_id = {ME}
                OR (
                    clearance_level = ANY ({CLEARANCES})
                    AND department_id = ANY ({DEPARTMENTS})
                    AND (
                        is_company_kb
                        OR visibility = 'department'
                    )
                );
        END;
        $$ LANGUAGE plpgsql STABLE
"""


def upgrade() -> None:
    # --- 1. what a personal document's visibility may be --------------------
    # Two statements before the constraint, and they are data repair rather than
    # ceremony: `ADD CONSTRAINT` validates every existing row, so a deployment whose
    # `documents` table holds a row the pair disagrees about would have its migration
    # fail half way through. Rows written before this revision were all derived by the
    # old `effective_visibility` (`'company' if is_company_kb else 'private'`), so the
    # repair below is expected to touch nothing — it exists so that a hand-written row
    # is corrected rather than left to block the deploy. It can only move a row *towards*
    # the rule: `is_company_kb` is never changed, and a personal row's `'company'` is
    # moved to `'private'`, which is the narrower of the two.
    op.execute(
        "UPDATE documents SET visibility = 'company' "
        "WHERE is_company_kb AND visibility <> 'company'"
    )
    op.execute(
        "UPDATE documents SET visibility = 'private' "
        "WHERE NOT is_company_kb AND visibility = 'company'"
    )
    # `visibility = 'company'` iff `is_company_kb`. See the module docstring: `private`
    # and `department` are the two degrees a person chooses between for their own file,
    # and `company` is what the kind flag derives rather than a third degree.
    op.execute(
        """
        ALTER TABLE documents
        ADD CONSTRAINT ck_documents_visibility_kind
        CHECK ((visibility = 'company') = is_company_kb)
        """
    )

    # --- 2. §5.2/Q29's marker on the answer ---------------------------------
    op.execute("ALTER TABLE rag_messages ADD COLUMN source_notice jsonb")

    # --- 3. the second line of defence, narrowed to the rule ----------------
    _drop_predicate_policies()
    op.execute(PREDICATE_SQL)
    _attach_predicate_policies()


def _drop_predicate_policies() -> None:
    """Drop the three policies that reference the predicate, before it is replaced.

    `document_chunks_access` first: its `USING` clause reads `documents`, so it is the
    one that would be left pointing at a dropped function if the order were reversed.
    """
    op.execute(f"DROP POLICY IF EXISTS {CHUNK_POLICY} ON document_chunks")
    op.execute(f"DROP POLICY IF EXISTS {DOCUMENT_WRITE_POLICY} ON documents")
    op.execute(f"DROP POLICY IF EXISTS {DOCUMENT_POLICY} ON documents")


def _attach_predicate_policies() -> None:
    """Ticket 13's predicate, attached to both tables, over the five-column signature.

    Recreated here rather than left in place because `DROP FUNCTION` takes the policies'
    working definition with it — see the module docstring. The clauses are the ones ticket
    31 wrote, with the predicate's two new arguments threaded through where a document's
    row is available.
    """
    op.execute(
        f"""
        CREATE POLICY {DOCUMENT_POLICY} ON documents
        FOR SELECT
        USING (
            document_visibility_predicate(
                owner_employee_id, department_id, clearance_level, is_company_kb, visibility
            )
            OR {SYSTEM}
        )
        """
    )
    # `UPDATE`'s `USING`/`WITH CHECK` are the read rule read forwards, as ticket 31 wrote
    # them. Unchanged by ticket 36: it adds no write rule.
    op.execute(
        f"""
        CREATE POLICY {DOCUMENT_WRITE_POLICY} ON documents
        FOR UPDATE
        USING (
            {SYSTEM}
            OR document_visibility_predicate(
                owner_employee_id, department_id, clearance_level, is_company_kb, visibility
            )
        )
        WITH CHECK (
            {SYSTEM}
            OR document_visibility_predicate(
                owner_employee_id, department_id, clearance_level, is_company_kb, visibility
            )
        )
        """
    )
    # The chunks, through their document — the join the design's §4.3 sketch also makes.
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
                      d.owner_employee_id, d.department_id, d.clearance_level,
                      d.is_company_kb, d.visibility
                  )
            )
        )
        WITH CHECK ({SYSTEM})
        """
    )


def downgrade() -> None:
    """Retrace the three changes, newest first.

    The predicate goes back to ticket 13's three-parameter body — which is *wider* than
    the rule, and that is the honest downgrade: it is what the schema said before this
    revision, and leaving the narrower one in place would make the down migration a
    behaviour change nobody asked for.
    """
    _drop_predicate_policies()
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION document_visibility_predicate(
            owner_employee_id uuid,
            department_id uuid,
            clearance_level text
        ) RETURNS boolean AS $$
        BEGIN
            RETURN
                owner_employee_id = {ME}
                OR (
                    clearance_level = ANY ({CLEARANCES})
                    AND department_id = ANY ({DEPARTMENTS})
                );
        END;
        $$ LANGUAGE plpgsql STABLE
        """
    )
    _attach_predicate_policies_three_arguments()

    op.execute("ALTER TABLE rag_messages DROP COLUMN IF EXISTS source_notice")
    op.execute(
        "ALTER TABLE documents DROP CONSTRAINT IF EXISTS ck_documents_visibility_kind"
    )


def _attach_predicate_policies_three_arguments() -> None:
    """Ticket 31's own policy definitions, restored verbatim for the down migration."""
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
    op.execute(
        f"""
        CREATE POLICY {DOCUMENT_WRITE_POLICY} ON documents
        FOR UPDATE
        USING (
            {SYSTEM}
            OR document_visibility_predicate(
                owner_employee_id, department_id, clearance_level
            )
        )
        WITH CHECK (
            {SYSTEM}
            OR document_visibility_predicate(
                owner_employee_id, department_id, clearance_level
            )
        )
        """
    )
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
