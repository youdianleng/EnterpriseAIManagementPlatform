"""Answer request and response shapes, and the SSE contract ticket 37 builds against.

**The stream is the answer, and it is not a `response_model`.** A streaming endpoint
returns a generator of frames rather than one document, so the shapes below describe the
*payloads* — what `ask_request` accepts and what each event carries — and the route
assembles the frames. `docs/DESIGN.md` §5.2 is 「流式 SSE」 and the ticket's first checklist
line is that the answer reaches the client incrementally, so a Pydantic response model
here would be a model nothing could satisfy without buffering.

**The event names and their payloads, in the order they arrive.** This is the wire
contract, and it is written out here because ticket 37 implements the client against it:

    event: start
      data: {message_id, conversation_id, question, model, provider, language}

    event: citations                     # before any text, so sources render first
      data: {citations: [CitationRead, ...],
             source_notice: SourceNoticeRead | null}    # ticket 36's marker

    event: delta                         # zero or more, one per increment
      data: {text}

    event: refusal                       # instead of citations/delta when D20 refuses
      data: {message_id, conversation_id, content, message_key,
             best_score, threshold, is_refusal: true, model_called: false,
             source_notice: null}                        # ticket 36: nothing to label

    event: error                         # instead of delta/done when the model failed
      data: {message_id, conversation_id, code, message_key, retryable}

    event: done                          # always the last event, on every branch
      data: {message_id, conversation_id, citations, source_notice, model, provider,
             token_in, token_out, latency_ms, is_refusal}

Every branch ends with `done` except `error`, which is terminal by itself: a client that
has been told the model failed has nothing left to wait for, and a `done` after it would
have to describe an answer that does not exist. `refusal` *is* followed by `done`, because
a refusal is a completed answer — D20's, with its own citations list (empty) and its own
`is_refusal: true`.

**`citations` is sent before the text on purpose.** A UI that renders the sources as soon
as they are known shows the reader where the answer will come from while the first tokens
are still being generated, and a UI that waited for the text would have to parse the
markers out of prose to build the list — which is exactly what `[N]` markers exist to
avoid.

**`source_notice` rides on `citations`, before any text** (ticket 36). §5.2's marker is
「回答顶部」 — at the top of the answer — so it has to reach a client that is still
streaming, and `citations` is the last frame that arrives before the first token. The
same object is repeated on `done` (so the record of what was stored is complete in one
frame) and returned by `GET /answers/conversations/{id}` on each message. `null` means
the answer is grounded only in the company knowledge base; a client that renders a banner
must treat `null` as "no banner" rather than as "unknown".

**The list, the rename and the delete are the conversation's owner's too** (ticket 37).
`ConversationSummaryRead` is deliberately not `ConversationRead`: a list of a person's
conversations is a list of *labels* — a title, when it was last used, and when D18 will
remove it — and shipping every message of every past conversation to draw a sidebar would
be a transcript nobody asked for. The detail read stays the one route that returns
messages, and it is reached by clicking a row.
"""

import json
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.api.v1.schemas.base import StrictModel
from app.domain.answer.models import AnswerEvent, Citation, EventKind

#: How long a name a person may give their own conversation. Longer than
#: `models.TITLE_CHARS`, because 80 is the width a *derived* title is clipped to so a
#: sidebar row stays one line, and somebody deliberately renaming a conversation may want
#: a sentence. The sidebar wraps rather than truncating either way (§3.1 forbids
#: truncation), so this is a ceiling on the storage and not a layout decision.
RENAME_MAX_CHARS = 120

#: The media type the route answers a stream with.
SSE_MEDIA_TYPE = "text/event-stream"


class AskRequest(BaseModel):
    """One question, and optionally the conversation to continue.

    `conversation_id` is optional because a first question has no conversation yet: the
    server creates one, titles it from the question, and the `start` event carries its id
    for every later turn. Requiring a client to create a conversation first would be an
    endpoint the design does not describe (§3.6's `rag_conversations` is created by the
    first message).
    """

    question: str = Field(min_length=1, max_length=2000)
    conversation_id: UUID | None = None


class CitationRead(BaseModel):
    """One citation: the file name, the page, the original snippet, and the link.

    `document_id` and `chunk_id` are what the client links *through*: the document id
    opens the original (the download route is guarded by the same §4.2 rule the search
    was), and the chunk id identifies the passage inside it. `page` is `null` for a format
    that has no pages — a text file, a spreadsheet, Markdown — and the client omits it
    rather than printing an invented one.
    """

    document_id: UUID
    chunk_id: UUID
    title: str
    filename: str
    #: `true` for the company knowledge base, `false` for somebody's personal upload.
    #: §5.2/Q29 requires the answer to be marked when it quotes a personal document.
    is_company_kb: bool
    page: int | None
    page_to: int | None
    heading_path: str | None
    #: `"parent"` or `"child"`: which text `quote` came from, so a client can say
    #: "the enclosing section" rather than "the passage".
    context_scope: str
    #: What a citation shows: the passage, exactly as the corpus holds it, never translated.
    quote: str
    #: The child's own text, which is what the ranking matched — where a highlight in the
    #: source should land.
    content: str
    rerank_score: float


class SourceNoticeRead(BaseModel):
    """§5.2/Q29's 「以下内容来自个人文档（非公司知识库）」 marker, as the wire carries it.

    Present exactly when at least one citation came from a personal document; `null`
    otherwise (ticket 36). `text` is the sentence in every language the interface ships,
    so a client with no dictionary still renders it, and `message_key`
    (`answer.source_notice.personal_document`) is how a client that *has* one renders its
    own copy. Ticket 37 draws the banner; this is the whole of what it needs to read.
    """

    personal_documents: bool
    message_key: str
    text: dict[str, str]


class MessageRead(BaseModel):
    """One stored answer, with everything the ticket asks a message to record.

    The accounting is on the response and not only in the database: an operator reading a
    conversation through the API needs the model, the tokens and the latency without a SQL
    client, and a UI that wants to show "answered by gpt-4o in 1.2 s" has the number.
    """

    id: UUID
    question: str
    content: str
    citations: list[CitationRead]
    model_used: str | None
    provider_used: str | None
    token_in: int
    token_out: int
    latency_ms: int
    #: D20's answer, which is an answer: `true` here means the knowledge base held no
    #: basis and no model was called.
    is_refusal: bool
    #: `ERR_ANS_001` when the model failed, else `null`. A client routes on this: a
    #: refusal offers "ask something else", a failure offers "retry".
    error_key: str | None
    status: str
    #: The permission predicate this answer was grounded under (§4.3's reviewability).
    retrieval_filter: str | None
    #: §5.2/Q29's marker, when the answer quoted a personal document (ticket 36). Read
    #: back with the message so a conversation from last week renders the same banner the
    #: streamed answer did, without re-deriving it from the citation flags.
    source_notice: SourceNoticeRead | None = None
    created_at: datetime


class PrefillFieldRead(BaseModel):
    """One field of a draft: what the submission will write, and how to draw it.

    The two labels travel with the field rather than being looked up by the client, the way
    `ToolAnswer` carries both sentences: the wording lives in `app/core/messages.py`, and a
    client that received only `label_key` would keep a second copy of it. `kind` is the input
    to draw (`date`, `time`, `text`, `textarea`, `number`, `select`) and `options` the choices
    of a select, each named in both languages.
    """

    name: str
    label_key: str
    kind: str
    label_es: str
    label_en: str
    value: str | int | None = None
    required: bool = True
    options: list[dict] = []
    hint_es: str | None = None
    hint_en: str | None = None


class PrefillFormRead(BaseModel):
    """Ticket 40's form, as the client draws it: the draft, and where it would be filed.

    An editable form rather than a summary — DESIGN §6.3's first requirement — so this is
    the tool's own `as_dict()` re-validated (the column is the record, this is the
    contract), not a projection somebody chose for the screen. `facts` is what the
    validation answered and is deliberately *not* a field list: nothing in it is editable,
    because nothing in it is written by a submission.
    """

    tool: str
    entity: str
    title_key: str
    title_es: str
    title_en: str
    submit_path: str
    fields: list[PrefillFieldRead]
    facts: dict


class DraftRead(BaseModel):
    """The conversation's newest draft, and whether it still stands.

    `status` is the *effective* one: a `proposed` row whose 24 hours have passed reads
    `expired` here, and the row itself is marked as expired when this read observes it
    (`domain/agent/service.py`). A client needs the difference — one offers "confirm", the
    other "generate it again" — and it is the database's clock that decides.

    **`resulting_entity_type` and `resulting_entity_id` are the document it became** (ticket
    41). §6.3's fourth requirement is that the audit carries the resulting entity, and these
    are read back from that same row rather than kept anywhere else: after a confirmation the
    employee sees a card that says so *and* a way to open what was created, and the id behind
    that link is the one the `agent_actions` row recorded. Both stay null for a rejection —
    「不产生任何单据」 as a fact about the response and not only about the row.
    """

    id: UUID
    tool_name: str
    status: str
    created_at: datetime
    expires_at: datetime
    prefill_form: PrefillFormRead | None = None
    #: The instant a person answered it, written by the database's clock. Null while
    #: `proposed` — the row's own constraint says so, and this is the same fact.
    confirmed_at: datetime | None = None
    #: What it became: the entity's type and id, or nothing for a rejection.
    resulting_entity_type: str | None = None
    resulting_entity_id: UUID | None = None


class ConversationRead(BaseModel):
    """A conversation and its messages, newest message last.

    **Ticket 40's draft rides here, and deliberately as a field rather than a route of its
    own.** A draft belongs to a conversation (§3.6 keys `agent_actions` by `conversation_id`
    and `user_id`), the read is already the caller's own conversation behind
    `session.read_own`, and a separate endpoint would be a second place the same ownership
    rule is spelled. The interface that shows the transcript is the interface that shows the
    form waiting inside it, and one request answers both.
    """

    id: UUID
    title: str
    created_at: datetime
    last_message_at: datetime
    expires_at: datetime
    messages: list[MessageRead]
    #: The conversation's newest draft, or `None` when the assistant never proposed one.
    draft: DraftRead | None = None


class ConversationSummaryRead(BaseModel):
    """One row of the conversation list: the label, and the two dates that matter.

    `last_message_at` is the list's ordering key and what the sidebar shows as "last
    used"; `expires_at` is D18's retention as it was written on the row, which is what
    lets the interface state the deadline (§5.1's 「界面上明确告知该期限」) with the
    server's date rather than one the client computed.
    """

    id: UUID
    title: str
    created_at: datetime
    last_message_at: datetime
    expires_at: datetime


class ConversationPageRead(BaseModel):
    """The caller's conversations, newest first, and how many there are in total.

    `total` travels with the page for the reason `DocumentPageRead`'s does: a client that
    shows "12 of 30" must not have to count by repeating a filter it cannot see.
    """

    items: list[ConversationSummaryRead]
    total: int


class ConversationRenameRequest(StrictModel):
    """A new name for one of the caller's conversations.

    Whitespace-only is refused here rather than by the database: `ck_rag_conversations_title`
    would raise an `IntegrityError`, which is a 500 for what is plainly a bad request. The
    strip-and-check below turns it into a 422 with the field named.
    """

    title: str = Field(min_length=1, max_length=RENAME_MAX_CHARS)

    @field_validator("title")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("a conversation's title cannot be blank")
        return cleaned


def citation_read(citation: Citation) -> CitationRead:
    return CitationRead(
        document_id=citation.document_id,
        chunk_id=citation.chunk_id,
        title=citation.title,
        filename=citation.filename,
        is_company_kb=citation.is_company_kb,
        page=citation.page,
        page_to=citation.page_to,
        heading_path=citation.heading_path,
        context_scope=citation.context_scope,
        quote=citation.quote,
        content=citation.content,
        rerank_score=citation.rerank_score,
    )


def citation_reads(citations: Sequence[Citation]) -> list[CitationRead]:
    return [citation_read(citation) for citation in citations]


def source_notice_read(stored: dict | None) -> SourceNoticeRead | None:
    """The stored marker as the contract, or `None` when the answer carries none.

    Re-validated through the model rather than passed through as raw JSONB, for the reason
    `_message_read` gives about citations: the column is the record and this is the
    contract, so a shape that drifted would fail here rather than in a client.
    """
    if not stored:
        return None
    return SourceNoticeRead.model_validate(stored)


def sse_frame(event: AnswerEvent) -> str:
    """One `AnswerEvent` as an SSE frame: `event:` line, `data:` line, blank line.

    A `delta` carries its increment in `event.text` rather than in `event.data` — that is
    what lets the streaming loop write a frame without building a payload dict — so the
    wire object is assembled here, in the one place that knows the wire format. Every other
    kind already carries `data` and is passed through. (The first version of this function
    sent `data: {}` for every delta: the text was on the event and the frame builder never
    looked at it, which is the kind of defect a test that only counted frames would miss.)

    `json.dumps` with `ensure_ascii=False`, because the corpus is Spanish and an answer is
    prose: escaping every accent to `\\u00e1` would triple the bytes on the wire for no
    benefit, and every SSE client reads UTF-8. Newlines inside a payload cannot break the
    framing either — JSON escapes them — which is why the payload is JSON rather than a
    bare sentence.
    """
    payload = {"text": event.text} if event.kind is EventKind.DELTA else event.data
    return f"event: {event.kind.value}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


__all__ = [
    "RENAME_MAX_CHARS",
    "SSE_MEDIA_TYPE",
    "AskRequest",
    "CitationRead",
    "ConversationPageRead",
    "ConversationRead",
    "ConversationRenameRequest",
    "ConversationSummaryRead",
    "EventKind",
    "MessageRead",
    "SourceNoticeRead",
    "citation_read",
    "citation_reads",
    "source_notice_read",
    "sse_frame",
]
