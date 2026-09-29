"""The ORM row for §3.6's `agent_actions`: what the assistant proposed, and its fate.

DESIGN §3.6 names the columns and this file is them, with two the design leaves to the
implementation and one decision worth reading:

**`expires_at` is a column, and it is written by the database.** §6.3 requires a draft to
lapse (「默认 24h」) and to be marked `expired` rather than merely filtered out; an expiry
computed on read would move every time the setting changed, and a row could not say what
it was proposed under. The insert computes it as `now() + make_interval(hours => :ttl)`,
so the instant is the *server's* clock — the same clock that later decides the row has
lapsed. A container whose clock is a second ahead of Postgres would otherwise confirm a
draft the database considered dead.

**`thread_id` is the LangGraph thread, kept beside the conversation.** §3.6 lists it, and
ticket 38 fixed what a thread is for this system: the conversation. It is stored rather
than derived because a resumed run is found *by* it (`graph.thread_config`), and a reader
holding a draft should be able to reach the paused run without guessing that today's
thread id is today's conversation id. It is nullable: a run without a checkpointer has no
thread, and that is a real state rather than a missing value.

**`confirmed_at`, `resulting_entity_type` and `resulting_entity_id` are ticket 41's to
write, and the constraints below are what keep them honest in the meantime.** A row
`proposed` may not name a resulting entity, a `confirmed` row must say when, and the
entity pair is both-or-neither — so "which document did this draft become" cannot be half
answered, and a draft cannot claim an outcome it has not had.

`produced_prefill_form` is nullable: the column is written for every draft this ticket
records, and ticket 41 records *rejections* of the same shape. A refusal that produced no
form is a row with the reason in `tool_output` and nothing in the form column.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db_metadata import Base

#: The statuses the column may take, written out here rather than imported from
#: `app.domain.agent.models.DraftStatus`. **A model module imports nothing from the
#: domain**: the domain's packages pull in services, and a service pulls in the audit
#: trail, which imports this module — an import cycle that surfaces as
#: "partially initialized module 'app.audit'" in whichever test imports first. The two
#: lists are held together by a test instead
#: (`tests/test_agent_draft_tools.py::test_the_column_and_the_enum_agree`), which is the
#: same arrangement the retrieval filter's vocabulary uses.
STATUS_VALUES: tuple[str, ...] = ("proposed", "confirmed", "rejected", "expired")


class AgentAction(Base):
    __tablename__ = "agent_actions"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    #: The conversation the draft belongs to. Cascades with it: a draft of a deleted
    #: conversation is not a document anybody can act on, and the confirm path (ticket 41)
    #: re-reads the conversation before it writes anything.
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("rag_conversations.id", ondelete="CASCADE"), nullable=False
    )
    #: The login that asked. §3.6 says `user_id` and the reason is ticket 34's: a
    #: conversation is a login's material, and a draft is what the assistant proposed
    #: inside one.
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The registry key of the draft tool that produced this — a constant, never a string
    #: a model typed (`ai/tools/registry.lookup` is the only door).
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: §3.6's two JSONB columns. They are the record of what the tool was asked and what it
    #: answered in structured form; §10.1 keeps them out of traces, and this table is the
    #: installation's own Postgres — the same place `tool_result` travels in the graph.
    tool_input: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    tool_output: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    produced_prefill_form: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default=text("'proposed'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resulting_entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resulting_entity_id: Mapped[UUID | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in STATUS_VALUES) + ")",
            name="ck_agent_actions_status",
        ),
        # A tool name is a registry key: never empty, and never a sentence. The registry
        # itself is what makes it one; this is the database's own refusal of a blank.
        CheckConstraint("length(btrim(tool_name)) > 0", name="ck_agent_actions_tool_name"),
        # The form is an object, not a sentence: a row holding prose in this column would
        # be a row every reader of §6.3's 「完整可编辑表单」 would have to special-case.
        CheckConstraint(
            "produced_prefill_form IS NULL "
            "OR jsonb_typeof(produced_prefill_form) = 'object'",
            name="ck_agent_actions_form_is_object",
        ),
        # A draft ends after it is proposed. All three fields are ticket 41's, and the
        # constraints are what make "proposed" mean "nothing has happened to it yet".
        CheckConstraint(
            "status <> 'proposed' OR (confirmed_at IS NULL AND resulting_entity_id IS NULL)",
            name="ck_agent_actions_proposed_is_open",
        ),
        CheckConstraint(
            "status <> 'confirmed' OR confirmed_at IS NOT NULL",
            name="ck_agent_actions_confirmed_has_instant",
        ),
        CheckConstraint(
            "(resulting_entity_type IS NULL) = (resulting_entity_id IS NULL)",
            name="ck_agent_actions_result_is_a_pair",
        ),
        CheckConstraint("expires_at > created_at", name="ck_agent_actions_expiry"),
        # The read the client makes: this conversation's newest drafts, and the pending
        # one among them.
        Index("ix_agent_actions_conversation", "conversation_id", "created_at"),
        # "What is waiting for me" across conversations, which is what ticket 41's
        # confirmation surface asks.
        Index("ix_agent_actions_pending", "user_id", "status"),
    )
