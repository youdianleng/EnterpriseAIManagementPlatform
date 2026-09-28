"""Conversations and messages: what one grounded answer has to record (ticket 34).

Revision ID: 0024
Revises: 0023
Created: 2026-10-05

**Chain position:** 0023 (`20261005_1000_timesheet_supplements.py`, ticket 29) → 0024
(this, ticket 34). The directory *and* `alembic heads` were read immediately before this
file was written — `heads` reported `0023` and nothing else — because the ids have
collided repeatedly in this project. There is no `0023` above this revision and no
second head.

Two tables (DESIGN §3.6), five policies, and the decisions worth reading:

* **`rag_messages.status` is a generated column**, `CASE WHEN completed_at IS NULL THEN
  'pending' WHEN is_refusal THEN 'refused' WHEN error_key IS NOT NULL THEN 'failed' ELSE
  'complete' END`. §3.6 has no `status` at all and the ticket asks for `is_refusal` and
  for the failure code; a hand-written status beside them would be a fourth field that can
  contradict the other three — a row saying `complete` *and* `is_refusal`, which is either
  the refusal's shape or a bug depending on which column a reader trusts. PostgreSQL
  derives it, so the contradiction is unrepresentable, and the four states stay
  index-able equality tests for the queries that will want them ("how many of last
  month's answers refused?"). `GENERATED ALWAYS … STORED` also means an INSERT that names
  the column is refused outright, which is the property that keeps it derived.

* **The question is on the assistant row, and there is no `role='user'` row.** §3.6's
  `role` is created with `'assistant'` as its server default and a CHECK that admits
  nothing else. One retrieval, one citation list and one token count belong together on
  the row of the answer they produced; a question stored separately would be a second row
  that could disagree about which answer it produced, and this ticket's transcript is the
  pair as one record. The CHECK is what makes that a database rule rather than a
  convention the next writer has to know.

* **`expires_at` is written, not derived, and nothing here deletes.** D18's 90 days is
  recorded per conversation as `created_at + 90 days`. A sweep job is a later ticket's;
  this migration deliberately ships no DELETE, because a retention policy that deletes
  evidence is the last thing to write by accident. The index on `expires_at` is what lets
  that sweep exist without a scan.

* **Five row-level policies, and the reason each direction is stated.** A conversation is
  its owner's, and a message is its conversation's owner's — the second needs a lookup, the
  same shape `document_chunks_access` uses for §4.2. Without an INSERT policy,
  `ENABLE ROW LEVEL SECURITY` plus a `FOR SELECT` policy refuses every INSERT (ticket 31
  found this on `documents` and recorded it there); without an UPDATE policy the
  completion is a silent no-op that still answers 200 (ticket 31 found that too). So each
  verb is declared where it belongs. The write rules are the read rule read forwards: you
  may write a conversation you may read.

  **`compliance` is in the read clauses, and it is the only role that is.** §3.7/D18 says
  「仅 compliance 角色可查」 about conversations, and §5.3 repeats it flatly while rejecting
  self-hosted LangSmith: 员工对话内容**只有 compliance 可查**. So the audit reader is the one
  cross-user clause — and `admin` is deliberately **not** in it, which is the one place this
  file's clauses are about more than ownership.

  The temptation to add `admin` ("somebody has to be able to repair a broken
  conversation") is refused for two reasons. §4.1 gives administration the system's
  *structure* — accounts, primary positions, departments, clearances — and denies it
  withheld *content* on separation-of-duties grounds (an administrator cannot read a
  payslip's contents either); a role that can read every employee's questions is a role
  that can read what they were worried about. And repair work does not need it: an operator
  repairs `rag_conversations` on the owner connection, which is exempt from the policy
  because a table's owner always is. Widening a row-level policy to make an administrative
  task convenient is exactly the direction this backstop must not err in.

  **Narrower than the application rule and never wider**, which is the direction a backstop
  must err in: compliance's read is the only cross-user clause, and it is the clause the
  design names by role. The consequence is deliberate and asserted in `tests/test_answer.py`
  against a real policy over a real connection: an administrator reading another user's
  conversation is refused at the database layer, not merely absent from a response.

* **The privileges are stated rather than inherited.** Migration 0007's
  `ALTER DEFAULT PRIVILEGES` would give `eam_app` everything, so `DELETE` is revoked on
  both tables: a conversation is removed by the flag the owner sets (§3.6's
  `deleted_by_user`), and an answer is evidence for the retention D18 imposes. The
  retention sweep that does delete will be a later migration's, on a connection that may
  say so.

**One constraint deliberately absent: "a complete answer has at least one citation".** The
ticket's other checklist half is 「有依据时引用不得为空」 and it is tempting to make that a
CHECK — but a model may legitimately reply "the passages provided do not answer this"
while retrieval *did* return passages above the threshold, and such an answer is
`complete` with nothing to cite. A database rule would refuse it and leave a `pending` row
for an answer that arrived, which is worse than the case it guards. So the guarantee is
enforced where it can be stated honestly: the retrieved passages are always sent as the
`citations` event **before any text**, so a client always has the sources regardless of
what the model wrote, and `tests/test_answer.py` asserts that list is non-empty whenever
retrieval found a basis.

No `GRANT` statement: migration 0007 set default privileges, so these two tables are
reachable by the runtime role without one — the same claim ticket 31's migration makes,
asserted for these tables in `tests/test_answer.py`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "eam_app"

#: The policy names, written out so `downgrade` drops exactly what `upgrade` created.
CONVERSATION_READ = "rag_conversations_read"
CONVERSATION_INSERT = "rag_conversations_insert"
CONVERSATION_UPDATE = "rag_conversations_update"
MESSAGE_READ = "rag_messages_read"
MESSAGE_INSERT = "rag_messages_insert"
MESSAGE_UPDATE = "rag_messages_update"

#: The current login, which is what a conversation belongs to. `app_setting` folds a
#: written-then-abandoned empty string into NULL (migration 0007), so a request with no
#: context reads as NULL, NULL is never equal to a uuid, and the row is refused — the
#: failure mode of a missing context is silence rather than disclosure.
ME = "app_setting('app.current_user_id')::uuid"

#: The roles that may read a conversation that is not their own. **`compliance` alone**, and
#: that is the design rather than a narrowing: §5.3 says 员工对话内容只有 compliance 可查 while
#: rejecting self-hosted LangSmith on exactly that ground, and §3.7/D18 gives compliance the
#: read-only conversation record. Administration is deliberately absent — §4.1 gives it the
#: system's *structure* and withholds personnel *content* (a payslip's contents included) —
#: and repair work needs no role at all, because the owner connection is exempt from the
#: policy. `tests/test_answer.py` asserts an admin is refused here.
AUDIENCES = "app_setting_array('app.current_roles') && ARRAY['compliance']"

#: Whether the row is *its* conversation's, from inside a message policy. The lookup is
#: what ticket 31's `document_chunks_access` does for §4.2 and for the same reason: a
#: message carries no user of its own, and giving it one would be a second column that
#: could disagree with the conversation's.
MESSAGE_OWNER = f"""
    EXISTS (
        SELECT 1 FROM rag_conversations c
         WHERE c.id = rag_messages.conversation_id
           AND c.user_id = {ME}
    )
"""

#: The generated status. See the module docstring: derived so the four states cannot
#: contradict the fields they are derived from.
STATUS_EXPRESSION = (
    "CASE WHEN completed_at IS NULL THEN 'pending' "
    "WHEN is_refusal THEN 'refused' "
    "WHEN error_key IS NOT NULL THEN 'failed' "
    "ELSE 'complete' END"
)


def upgrade() -> None:
    op.create_table(
        "rag_conversations",
        sa.Column("id", sa.UUID(), nullable=False),
        # The login, not the employee: D18's retention is about conversations and a
        # conversation is read back through a session, exactly as ticket 19 recorded for
        # notifications.
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_message_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Written by the service as `created_at + RAG_RETENTION_DAYS`; see the docstring
        # for why it is a column rather than a computed read.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "deleted_by_user",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.CheckConstraint("length(btrim(title)) > 0", name="ck_rag_conversations_title"),
        # No employee id and no user id can be invented; the row names a real login.
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # The conversation list: mine, newest first.
    op.create_index(
        "ix_rag_conversations_user", "rag_conversations", ["user_id", "last_message_at"]
    )
    # The retention sweep's query. Its own index because the sweep runs over the whole
    # table for every deployment, and a scan there is a scan of everybody's transcripts.
    op.create_index("ix_rag_conversations_expires", "rag_conversations", ["expires_at"])

    op.create_table(
        "rag_messages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column(
            "role",
            sa.String(length=12),
            nullable=False,
            server_default=sa.text("'assistant'"),
        ),
        # The question lives on the answer's row: see the module docstring.
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=sa.text("''")),
        # §3.6's two JSONB columns, plus the filter the run applied (below).
        sa.Column(
            "citations",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "retrieval_debug",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # §4.3's 「检索调试视图中显示本次生效的权限条件，便于人工复核」, kept on the row so a
        # message can still be explained after the request is gone. A column rather than a
        # field inside `retrieval_debug` because "which answers were grounded under a
        # permissive predicate" has to be a query, and a JSONB field is not one without an
        # expression index nobody would think to add.
        sa.Column("retrieval_filter", sa.Text(), nullable=True),
        # §5.3: which model answered, and through which adapter.
        sa.Column("model_used", sa.String(length=64), nullable=True),
        sa.Column("provider_used", sa.String(length=32), nullable=True),
        sa.Column("token_in", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("token_out", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "is_refusal", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("error_key", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # NULL while the answer is still being written, which is what makes a row left
        # `pending` by a dropped connection visible.
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        # Derived. See the module docstring and `STATUS_EXPRESSION`.
        sa.Column(
            "status",
            sa.String(length=12),
            sa.Computed(STATUS_EXPRESSION, persisted=True),
            nullable=False,
        ),
        sa.CheckConstraint("role = 'assistant'", name="ck_rag_messages_role"),
        sa.CheckConstraint("length(btrim(question)) > 0", name="ck_rag_messages_question"),
        sa.CheckConstraint("token_in >= 0", name="ck_rag_messages_token_in"),
        sa.CheckConstraint("token_out >= 0", name="ck_rag_messages_token_out"),
        sa.CheckConstraint("latency_ms >= 0", name="ck_rag_messages_latency"),
        sa.CheckConstraint(
            "status IN ('pending', 'complete', 'refused', 'failed')",
            name="ck_rag_messages_status",
        ),
        # A refusal is *text* — both languages of it — so a refused row with no content is
        # a row a client would render as blank. A failure is the one terminal state that
        # may have nothing to show, and it is distinguished by `error_key` below.
        sa.CheckConstraint(
            "status <> 'refused' OR length(btrim(content)) > 0",
            name="ck_rag_messages_refusal_has_text",
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR error_key IS NOT NULL",
            name="ck_rag_messages_failed_has_code",
        ),
        # A refusal is D20's ordinary answer and a failure is an incident; a row that
        # claimed both would make the client's routing ("ask something else" or "retry")
        # a coin toss.
        sa.CheckConstraint(
            "is_refusal = false OR error_key IS NULL",
            name="ck_rag_messages_refusal_not_failed",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["rag_conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # The reading order of one conversation.
    op.create_index(
        "ix_rag_messages_conversation", "rag_messages", ["conversation_id", "created_at"]
    )

    _attach_policies()


def _attach_policies() -> None:
    """The six policies: two tables, three verbs each, one rule per verb.

    Written out per verb rather than as one `FOR ALL`, for the reason ticket 31 recorded
    when it found the failures: a permissive `ALL` policy carries a `WITH CHECK` that every
    write is measured against, so an `ALL` whose check is false silently refuses *every*
    insert and update — and an `ALL` whose check is true is a write rule nobody can read.
    Each verb below says what it admits and nothing else.
    """
    op.execute("ALTER TABLE rag_conversations ENABLE ROW LEVEL SECURITY")
    # Reading is ownership, with the two audiences §3.7/D18 name.
    op.execute(
        f"""
        CREATE POLICY {CONVERSATION_READ} ON rag_conversations
        FOR SELECT
        USING (user_id = {ME} OR {AUDIENCES})
        """
    )
    # Writing is the same rule read forwards: you may create a conversation you could then
    # read. Without this the INSERT is refused outright — row-level security applies the
    # SELECT policy's `USING` clause to the new row when no INSERT policy exists.
    op.execute(
        f"""
        CREATE POLICY {CONVERSATION_INSERT} ON rag_conversations
        FOR INSERT
        WITH CHECK (user_id = {ME})
        """
    )
    # `last_message_at` moves for every terminal state, so the update rule is ownership
    # again. Without it the move is a silent no-op and the endpoint still answers 200.
    op.execute(
        f"""
        CREATE POLICY {CONVERSATION_UPDATE} ON rag_conversations
        FOR UPDATE
        USING (user_id = {ME})
        WITH CHECK (user_id = {ME})
        """
    )

    op.execute("ALTER TABLE rag_messages ENABLE ROW LEVEL SECURITY")
    # A message is reachable through its conversation and nowhere else: it carries no user
    # of its own, so there is no second column to keep in step.
    op.execute(
        f"""
        CREATE POLICY {MESSAGE_READ} ON rag_messages
        FOR SELECT
        USING ({MESSAGE_OWNER} OR {AUDIENCES})
        """
    )
    op.execute(
        f"""
        CREATE POLICY {MESSAGE_INSERT} ON rag_messages
        FOR INSERT
        WITH CHECK ({MESSAGE_OWNER})
        """
    )
    op.execute(
        f"""
        CREATE POLICY {MESSAGE_UPDATE} ON rag_messages
        FOR UPDATE
        USING ({MESSAGE_OWNER})
        WITH CHECK ({MESSAGE_OWNER})
        """
    )

    # A conversation is removed by the flag its owner sets (§3.6's `deleted_by_user`), and
    # an answer is evidence for the retention D18 imposes. So the runtime role does not
    # hold DELETE on either table: a later migration's sweep, on a connection that may say
    # who it is, is where a delete belongs.
    op.execute(f"REVOKE DELETE ON rag_conversations FROM {APP_ROLE}")
    op.execute(f"REVOKE DELETE ON rag_messages FROM {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {MESSAGE_UPDATE} ON rag_messages")
    op.execute(f"DROP POLICY IF EXISTS {MESSAGE_INSERT} ON rag_messages")
    op.execute(f"DROP POLICY IF EXISTS {MESSAGE_READ} ON rag_messages")
    op.execute("ALTER TABLE rag_messages DISABLE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {CONVERSATION_UPDATE} ON rag_conversations")
    op.execute(f"DROP POLICY IF EXISTS {CONVERSATION_INSERT} ON rag_conversations")
    op.execute(f"DROP POLICY IF EXISTS {CONVERSATION_READ} ON rag_conversations")
    op.execute("ALTER TABLE rag_conversations DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_rag_messages_conversation", table_name="rag_messages")
    op.drop_table("rag_messages")
    op.drop_index("ix_rag_conversations_expires", table_name="rag_conversations")
    op.drop_index("ix_rag_conversations_user", table_name="rag_conversations")
    op.drop_table("rag_conversations")
