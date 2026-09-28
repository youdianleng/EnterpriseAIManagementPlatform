"""ORM rows for the answer module: the conversation, and the messages in it.

`docs/DESIGN.md` §3.6 fixes both tables, and the columns are the design's — `citations`,
`model_used`, `provider_used`, `token_in`, `token_out`, `latency_ms`, `retrieval_debug`
and `is_refusal` are on `rag_messages` because ticket 34's sixth checklist line asks for
exactly those facts about every message. Four decisions are worth reading:

* **The conversation is `user_id`'s, not the employee's.** §3.6 says `user_id` and the
  reason it is right is the same one ticket 19 recorded for notifications: a conversation
  is a *login's* material — it is read back through a session, and D18's 「仅 compliance 角色可查」
  is a statement about accounts. `rag_messages` therefore carries no user of its own; it
  reaches one through its conversation, which is what keeps "whose conversation is this"
  a single column rather than two that could disagree.

* **`expires_at` is `created_at + 90 days`, computed by the database.** D18's retention is
  90 days and this is the column that records it. It is written by the service rather than
  derived on read, so changing the retention period is a decision recorded on the rows
  that were written under the old one rather than a number that silently reinterprets
  history. Nothing in this ticket deletes an expired conversation: the sweep is a job, and
  a ticket that ships a delete nobody asked for is a ticket that loses evidence.

* **`status` is a *generated* column derived from `is_refusal` and `error_key`, and it is
  not stored twice.** §3.6 has neither, and the ticket asks for 「是否为拒答」 and for the
  failure code; a hand-written `status` beside them would be a fourth field that can
  disagree with the other three — a row saying `complete` and `is_refusal` at once. As a
  generated column the database derives it, so the four states (`pending`, `complete`,
  `refused`, `failed`) cannot contradict the facts they are derived from, and a query
  ("how many of last month's answers refused?") is still an index-able equality test.

* **The whole answer is in `content`, and the row is written before the first byte is
  sent.** The client cannot wait for the stream to end to learn the message's id, so the
  row exists in `pending` from the start and is completed at the end. That is also what
  makes a dropped connection leave evidence — a `pending` row that nobody finished is
  visible, where a row written only on success would make the failure invisible.

**A note on `retrieval_filter`.** It is not in §3.6's table and it is here because §4.3
requires the effective permission condition to be reviewable 「便于人工复核」: the debug
view shows it for the run you just made, and this column is how a message from last month
can still say which predicate grounded it. `retrieval_debug` carries it as well, and the
duplication is deliberate — the column can be indexed and queried across messages ("which
answers were grounded under a permissive predicate"), which a field inside JSONB cannot be
without an expression index nobody would find.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db_metadata import Base

#: D18's retention, in days. A constant rather than a setting for the reason
#: `docs/DESIGN.md` states: 90 days is the design's decision, and a deployment that wants
#: another number changes the design.
RETENTION_DAYS = 90

#: The states the generated `status` column can take. Named here so the application's
#: tests and the migration's constraint are readable against one list, and so
#: `Computed`'s expression below can be explained in one place: a message is `pending`
#: until it is completed, `refused` when D20 refused it, `failed` when `error_key` records
#: a model failure, and `complete` otherwise.
MESSAGE_STATUSES = ("pending", "complete", "refused", "failed")


class RagConversation(Base):
    __tablename__ = "rag_conversations"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: D18: 「用户可自行删除」. A soft flag rather than a delete, because the row is evidence
    #: for the retention the design imposes on the *other* reader — compliance — and a
    #: person removing a conversation from their own list is not a reason to destroy the
    #: record that it existed.
    deleted_by_user: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    __table_args__ = (
        # A title is a label in a list. An empty one is a row nobody can identify, which
        # is what `title_for` exists to prevent; the database says so as well.
        CheckConstraint("length(btrim(title)) > 0", name="ck_rag_conversations_title"),
        # The conversation list: mine, newest first, without the ones I removed.
        Index("ix_rag_conversations_user", "user_id", "last_message_at"),
    )


class RagMessage(Base):
    __tablename__ = "rag_messages"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("rag_conversations.id", ondelete="CASCADE"), nullable=False
    )
    #: One value, because this table holds answers only: the question travels on the
    #: assistant row as `question`, which is what keeps one retrieval, one citation list
    #: and one token count together on the row that used them. A second `role='user'` row
    #: would be the same facts split across two rows that could disagree about which
    #: question produced which answer.
    role: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default=text("'assistant'")
    )

    question: Mapped[str] = mapped_column(Text, nullable=False)
    #: The answer, or the refusal text. Empty while `pending`, and empty for a `failed`
    #: message — there is no half-answer to store, which is the ticket's point.
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    #: §3.6's `citations`: the list the client renders, each entry carrying the file name,
    #: the page, the quoted passage and the ids a citation links through.
    citations: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    #: 「命中的 chunk id 与分数」 — plus the threshold it was measured against and the legs
    #: that could run, because a stored score with no threshold cannot be reviewed.
    retrieval_debug: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    #: The permission predicate this answer was grounded under. See the module docstring.
    retrieval_filter: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: §5.3: which model answered, and which adapter. Both, because "gpt-4o via openai" and
    #: "gpt-4o via a local gateway" are different facts, and §5.3 requires a degradation to
    #: be visible in this column.
    model_used: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_used: Mapped[str | None] = mapped_column(String(32), nullable=True)

    token_in: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    token_out: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    is_refusal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    #: The catalogue code of a model failure (`ERR_ANS_001`), or NULL. `is_refusal` cannot
    #: stand in for it: a refusal is D20's ordinary answer and a failure is an incident,
    #: and a client routes on the difference — one offers "ask something else", the other
    #: offers "retry".
    error_key: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: Derived, never written (see the module docstring). `Computed` renders
    #: `GENERATED ALWAYS AS (…) STORED`, which is what makes the four states impossible to
    #: contradict: `completed_at` is the discriminator between a row still being written
    #: and one that finished, and PostgreSQL refuses an INSERT or UPDATE that writes this
    #: column at all.
    status: Mapped[str] = mapped_column(
        String(12),
        Computed(
            "CASE WHEN completed_at IS NULL THEN 'pending' "
            "WHEN is_refusal THEN 'refused' "
            "WHEN error_key IS NOT NULL THEN 'failed' "
            "ELSE 'complete' END",
            persisted=True,
        ),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint("role = 'assistant'", name="ck_rag_messages_role"),
        CheckConstraint("length(btrim(question)) > 0", name="ck_rag_messages_question"),
        CheckConstraint("token_in >= 0", name="ck_rag_messages_token_in"),
        CheckConstraint("token_out >= 0", name="ck_rag_messages_token_out"),
        CheckConstraint("latency_ms >= 0", name="ck_rag_messages_latency"),
        CheckConstraint(
            "status IN ('pending', 'complete', 'refused', 'failed')",
            name="ck_rag_messages_status",
        ),
        CheckConstraint(
            # The one terminal state that may leave `content` empty is a failure: a
            # refusal never may, because D20's refusal *is* text, in both languages of it.
            "status <> 'refused' OR length(btrim(content)) > 0",
            name="ck_rag_messages_refusal_has_text",
        ),
        CheckConstraint(
            "status <> 'failed' OR error_key IS NOT NULL",
            name="ck_rag_messages_failed_has_code",
        ),
        # The client renders the refusal from the catalogue, and it must be able to tell
        # D20's answer from a model failure with one field.
        CheckConstraint(
            "is_refusal = false OR error_key IS NULL",
            name="ck_rag_messages_refusal_not_failed",
        ),
        # The reading order of a conversation, and the index the retention sweep needs.
        Index("ix_rag_messages_conversation", "conversation_id", "created_at"),
    )
