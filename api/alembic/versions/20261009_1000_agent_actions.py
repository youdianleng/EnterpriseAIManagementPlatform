"""`agent_actions`: the durable record of what the assistant proposed (ticket 40).

Revision ID: 0027
Revises: 0026
Created: 2026-10-09

**Chain position:** 0026 (`20261008_1000_langgraph_checkpoints.py`, ticket 38) → 0027
(this, ticket 40). The directory *and* `alembic heads` were read immediately before this
file was written — `heads` reported `0026` and nothing else — because the ids have
collided repeatedly in this project.

**What this table is for.** DESIGN §6.3's fourth requirement: 「`agent_actions` 表全程留痕：
工具入参、出参、生成的草稿、确认状态、最终实体 ID」. §3.6 lists it among the agent tables
and D22 makes it the core of "the agent never writes the database": the *draft* is a row
here, and the document it becomes (ticket 41) is written by the platform after a human
clicked a button. Ticket 40 writes one row per draft and reads it back; ticket 41 writes
`confirmed_at`, `resulting_entity_type` and `resulting_entity_id`.

**Two columns §3.6 does not list, and why each is here.**

* `created_at` — every table in this schema has one, and the pair with `expires_at` is
  what lets a reader see what a draft was proposed *under*.
* `expires_at` — §6.3's 「草稿有**过期时间**（默认 24h），过期后 `status=expired`」. The
  insert computes it in SQL (`now() + make_interval(hours => :ttl)`), so the instant is the
  database's clock and the comparison that later expires the row uses the same clock. A
  process-side `datetime.now()` would disagree with Postgres by whatever the container's
  clock is off by, and the disagreement would be a draft confirmed after it lapsed.

**Three constraints are the design's own rules made unrepresentable-in-the-wrong-shape.**
The form column must be a JSON *object* (a sentence in it would be a form no client could
draw); a `proposed` row may name no outcome (`confirmed_at` and `resulting_entity_id` both
NULL), which is what makes "proposed" mean "nothing has happened to it yet"; and a
`confirmed` row must say when. The entity pair is both-or-neither, so "which document did
this become" cannot be half answered. None of these is a policy the application could
forget: PostgreSQL refuses the row.

**The row-level policy is `rag_conversations`' rule, applied to its drafts.** A draft is
conversation material — §3.6 gives the conversation to its `user_id`, and this row carries
the same two keys. So SELECT, INSERT and UPDATE are all "the row is mine", written out per
verb for the reason ticket 34 recorded: a permissive `FOR ALL` carries one `WITH CHECK`
that every write is measured against, and an `ALL` whose check is wrong either refuses
every insert or is a write rule nobody can read. **`DELETE` is revoked**, like the
conversation and the answer beside it: this table is D22's audit trail, and an audit trail
the request role may delete is not one.

**No `GRANT` statement**: migration 0007's `ALTER DEFAULT PRIVILEGES` gives the runtime
role DML on tables created later in `public`, which is the claim
`tests/test_permission_matrix.py` asserts over a scratch table. Only the one *narrowing* —
`REVOKE DELETE` — is written here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spelled here as migration 0007 spells it: a migration describes the schema it applied,
#: and the application's constant for the same name may move afterwards.
APP_ROLE = "eam_app"

#: The policy names, written out so `downgrade` drops exactly what `upgrade` created.
READ = "agent_actions_read"
INSERT = "agent_actions_insert"
UPDATE = "agent_actions_update"

#: The current login, which is what a draft belongs to. `app_setting` folds a
#: written-then-abandoned empty string into NULL (migration 0007), so a request with no
#: context reads as NULL, NULL is never equal to a uuid, and the row is refused — the
#: failure mode of a missing context is silence rather than disclosure.
ME = "app_setting('app.current_user_id')::uuid"

#: The four statuses, written as the literal they were applied with rather than imported
#: from `app.domain.agent.models`: a migration must describe the schema of its own moment.
STATUSES = ("proposed", "confirmed", "rejected", "expired")


def upgrade() -> None:
    op.create_table(
        "agent_actions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        # The LangGraph thread the run paused on (§3.6). Nullable: a run without a
        # checkpointer has no thread id, and that is a state rather than a missing value.
        sa.Column("thread_id", sa.String(length=64), nullable=True),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column(
            "tool_input",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "tool_output",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("produced_prefill_form", postgresql.JSONB(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=12),
            nullable=False,
            server_default=sa.text("'proposed'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Written by the insert in SQL; see the module docstring.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resulting_entity_type", sa.String(length=32), nullable=True),
        sa.Column("resulting_entity_id", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in STATUSES) + ")",
            name="ck_agent_actions_status",
        ),
        sa.CheckConstraint("length(btrim(tool_name)) > 0", name="ck_agent_actions_tool_name"),
        sa.CheckConstraint(
            "produced_prefill_form IS NULL "
            "OR jsonb_typeof(produced_prefill_form) = 'object'",
            name="ck_agent_actions_form_is_object",
        ),
        sa.CheckConstraint(
            "status <> 'proposed' OR (confirmed_at IS NULL AND resulting_entity_id IS NULL)",
            name="ck_agent_actions_proposed_is_open",
        ),
        sa.CheckConstraint(
            "status <> 'confirmed' OR confirmed_at IS NOT NULL",
            name="ck_agent_actions_confirmed_has_instant",
        ),
        sa.CheckConstraint(
            "(resulting_entity_type IS NULL) = (resulting_entity_id IS NULL)",
            name="ck_agent_actions_result_is_a_pair",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_agent_actions_expiry"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["rag_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # The read the client makes: this conversation's drafts, newest first.
    op.create_index(
        "ix_agent_actions_conversation", "agent_actions", ["conversation_id", "created_at"]
    )
    # "What is waiting for me", across conversations: ticket 41's confirmation surface.
    op.create_index("ix_agent_actions_pending", "agent_actions", ["user_id", "status"])

    _attach_policies()

    # D22's trail, and the same narrowing tickets 24 and 34 applied to their own tables: a
    # row the request role may delete is a record the audit does not have.
    op.execute(f"REVOKE DELETE ON agent_actions FROM {APP_ROLE}")


def _attach_policies() -> None:
    """Ownership, written out per verb. See the module docstring."""
    op.execute("ALTER TABLE agent_actions ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {READ} ON agent_actions
        FOR SELECT
        USING (user_id = {ME})
        """
    )
    # The same rule read forwards. Without an INSERT policy, row-level security applies the
    # SELECT policy's `USING` clause to the new row and refuses every insert.
    op.execute(
        f"""
        CREATE POLICY {INSERT} ON agent_actions
        FOR INSERT
        WITH CHECK (user_id = {ME})
        """
    )
    # Ticket 41 moves `status` and writes the three outcome columns. Without this the move
    # is a silent no-op and the endpoint still answers 200 — the failure ticket 34 recorded
    # for `rag_conversations`.
    op.execute(
        f"""
        CREATE POLICY {UPDATE} ON agent_actions
        FOR UPDATE
        USING (user_id = {ME})
        WITH CHECK (user_id = {ME})
        """
    )


def downgrade() -> None:
    """Drop the table and everything with it.

    The table *is* the feature: D22's audit of what the assistant proposed has no meaning
    without the drafts, and leaving a table no revision creates would make `upgrade` after
    `downgrade` a different operation than `upgrade` on a fresh database. A downgrade of
    ticket 40 is a decision to discard every unconfirmed draft in the installation.
    """
    op.execute(f"DROP POLICY IF EXISTS {UPDATE} ON agent_actions")
    op.execute(f"DROP POLICY IF EXISTS {INSERT} ON agent_actions")
    op.execute(f"DROP POLICY IF EXISTS {READ} ON agent_actions")
    op.drop_index("ix_agent_actions_pending", table_name="agent_actions")
    op.drop_index("ix_agent_actions_conversation", table_name="agent_actions")
    op.drop_table("agent_actions")
