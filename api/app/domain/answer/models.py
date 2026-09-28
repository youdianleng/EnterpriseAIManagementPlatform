"""What an answer is made of, as values.

`docs/DESIGN.md` §5.2 fixes the pipeline and §3.6 fixes what has to survive it. This
module holds the shapes that travel between the pieces, and four decisions are worth
reading before the code:

* **A citation is a `SearchHit` projected down, not the hit itself.** The design's
  citation is `《文件名》第 N 页` plus the passage it quotes, and the client has to be able
  to *link* it back to the source — so `document_id`, `chunk_id` and `page_from` travel.
  What does not travel is every score the ranking produced: `vector_distance`,
  `fusion_score` and the rest belong to the retrieval debug view, and a citation is a
  statement about a document rather than about a query. Keeping the two apart is what
  stops `citations` (read by the user) and `retrieval_debug` (read by an operator) from
  becoming one column nobody can decide the audience of.

* **`page` is a single nullable number, and `page_to` is what disambiguates it.** A
  chunk that spans a page break has a range; §5.2's format names one page, so
  `Citation.page` is `page_from` and `page_to` is carried beside it, because silently
  dropping the fact that a passage continues onto the next page would make a citation
  wrong in a way a reader cannot see. A Markdown file has no pages at all and both are
  `None`: the honest value, never an invented "p. 1" — `schemas/retrieval.py` made the
  same call for the same reason.

* **`AnswerEvent` is one flat value with a `kind`, rather than a class per event.**
  The SSE contract ticket 37 builds against is then readable from a single file, the
  terminal event is one object with a `failure`/`refusal`/`answer` outcome rather than
  three types the client has to tell apart by shape, and the driver can be asserted
  against a list of events. The alternative — a closed union of dataclasses — would put
  the wire contract in three places.

* **`AskOutcome` is a value even when it is a failure.** The ticket's 「界面显示明确的错误与重试
  入口」 is an outcome the driver *reports* rather than an exception it raises, because by
  the time the model has failed the SSE response has already sent its first byte and no
  status code can change. The `ErrorCode` travels on it, so the same code reaches the
  client through a normal response envelope (a question with no basis) and through the
  stream's last event (a model that failed).
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from app.core.errors import ErrorCode
from app.domain.retrieval.models import SearchHit

#: The languages an answer is written in, for the record. Two, because the corpus is
#: Spanish and English (§10.4) and a question in any other language is answered in that
#: language but stored under a name this system knows. `OTHER` is not a fallback for
#: "we could not tell": an answer in Chinese is a real answer, and the column says so
#: rather than forcing a wrong `es`.
DEFAULT_ANSWER_LANGUAGE = "es"

#: How much of the question becomes a conversation's title. A title is a label in a
#: list, and §3.6's `title` exists so a 90-day-old conversation can be recognised; the
#: whole question would be a body in a heading.
TITLE_CHARS = 80


class AnswerLanguage(StrEnum):
    """Which language an answer is written in — decided by the *question*'s.

    §5.2's rules: 「回答语言跟随提问语言；引用原文不翻译」. So this is a fact about the
    question, detected once and carried on the prompt, and the prompt states the second
    half explicitly (quoted text is never translated) because a model asked to answer
    in English about a Spanish policy will otherwise helpfully translate the passage it
    quotes — and a translated quote is no longer a citation.
    """

    ES = "es"
    EN = "en"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Citation:
    """One passage the answer is grounded in, as the client needs to show and open it.

    `quote` is the passage as the corpus holds it — the parent's context when the split
    gave one, which is what `SearchHit.quote` already resolves — and it is never
    translated (§5.2). `content` is the child's own text, kept because it is what the
    ranking matched and therefore what a highlight in the source should mark.

    `is_company_kb` travels because §5.2 asks the answer to carry the 「以下内容来自个人文档
    （非公司知识库）」 banner (Q29) and the document's own flag is the only thing that decides
    it. Ticket 36 owns *rendering* that banner and the retrieval pool rule that goes with
    it — a personal document must not be recalled from the company pool at all — so what
    this field does here is make the answer capable of saying which side a citation came
    from, rather than making the decision for a ticket that has not been written.
    """

    document_id: UUID
    chunk_id: UUID
    title: str
    filename: str
    is_company_kb: bool
    page: int | None
    page_to: int | None
    heading_path: str | None
    context_scope: str
    quote: str
    content: str
    rerank_score: float

    @classmethod
    def of(cls, hit: SearchHit) -> "Citation":
        return cls(
            document_id=hit.document.id,
            chunk_id=hit.chunk_id,
            title=hit.document.title,
            filename=hit.document.filename,
            is_company_kb=hit.document.is_company_kb,
            page=hit.page_from,
            page_to=hit.page_to,
            heading_path=hit.heading_path,
            context_scope=hit.context_scope,
            quote=hit.quote,
            content=hit.content,
            rerank_score=hit.rerank_score,
        )


@dataclass(frozen=True, slots=True)
class CitationDebug:
    """One hit's identifiers and scores, for `rag_messages.retrieval_debug`.

    A compact projection rather than the whole `SearchOutcome`: the design calls for
    「命中的 chunk id 与分数」, and the full candidate waterfall — both legs' top twenty,
    the fusion arithmetic, why each candidate was dropped — is what `GET
    /retrieval/debug` renders for the roles authorised to read the corpus's internals.
    Storing that here would put a view's payload in a per-message column and leave two
    renderings of one run to keep in step.

    What it must carry is enough to answer "which passages grounded this answer, and how
    strongly", which is the question an incident review asks of a message.
    """

    chunk_id: UUID
    document_id: UUID
    filename: str
    page: int | None
    context_scope: str
    fusion_score: float
    rerank_score: float

    @classmethod
    def of(cls, hit: SearchHit) -> "CitationDebug":
        return cls(
            chunk_id=hit.chunk_id,
            document_id=hit.document.id,
            filename=hit.document.filename,
            page=hit.page_from,
            context_scope=hit.context_scope,
            fusion_score=hit.fusion_score,
            rerank_score=hit.rerank_score,
        )


@dataclass(frozen=True, slots=True)
class RetrievalDebug:
    """What the retrieval half of one message looked like, as the JSONB column holds it.

    `filter_explanation` is the predicate the run actually applied — §4.3's 「检索调试视图中
    显示本次生效的权限条件」 — and it is `None` only for a run that genuinely had no
    filter, which the answer path never has: `filtered` is asserted false here so that a
    message stored without a permission condition is visible as the defect it is rather
    than indistinguishable from one stored with a permissive condition.
    """

    query: str
    filtered: bool
    filter_explanation: str | None
    legs_used: tuple[str, ...]
    embedder: str | None
    threshold: float
    best_score: float
    hits: tuple[CitationDebug, ...]

    def as_json(self) -> dict[str, Any]:
        """The column's shape. JSON-native values only: psycopg will not guess a uuid."""
        return {
            "query": self.query,
            "filtered": self.filtered,
            "filter_explanation": self.filter_explanation,
            "legs_used": list(self.legs_used),
            "embedder": self.embedder,
            "threshold": self.threshold,
            "best_score": self.best_score,
            "hits": [
                {
                    "chunk_id": str(hit.chunk_id),
                    "document_id": str(hit.document_id),
                    "filename": hit.filename,
                    "page": hit.page,
                    "context_scope": hit.context_scope,
                    "fusion_score": hit.fusion_score,
                    "rerank_score": hit.rerank_score,
                }
                for hit in self.hits
            ],
        }


def citations_json(citations: tuple[Citation, ...]) -> list[dict[str, Any]]:
    """`citations` as the JSONB array `rag_messages.citations` holds (DESIGN §3.6)."""
    return [
        {
            "document_id": str(citation.document_id),
            "chunk_id": str(citation.chunk_id),
            "title": citation.title,
            "filename": citation.filename,
            "is_company_kb": citation.is_company_kb,
            "page": citation.page,
            "page_to": citation.page_to,
            "heading_path": citation.heading_path,
            "context_scope": citation.context_scope,
            "quote": citation.quote,
            "content": citation.content,
            "rerank_score": citation.rerank_score,
        }
        for citation in citations
    ]


class EventKind(StrEnum):
    """The SSE event names. The wire contract ticket 37 builds against.

    Six, and the order they arrive in is part of the contract:

        start     → ids and the model that will answer
        citations → every citation, before any text (so the UI can render the sources)
        delta     → one increment of the answer, zero or more times
        refusal   → the whole refusal, in both languages, and no `delta` will follow
        error     → the model failed; retryable, and no `delta` will follow
        done      → the record of what was stored, and the end of the stream

    `refusal` and `error` are distinct names rather than one `outcome` with a flag,
    because they are distinct screens: a refusal is the honest answer §5.2/D20 asks
    for, and a model failure is an incident the person retries. A client that had to
    read a payload to tell them apart would eventually render one as the other.
    """

    START = "start"
    CITATIONS = "citations"
    DELTA = "delta"
    REFUSAL = "refusal"
    ERROR = "error"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class AskFailure:
    """A model call that did not produce an answer. The ticket's explicit error.

    `retryable` is a field rather than something the client infers from the code: the
    ticket asks for 「明确的错误与重试入口」, and "retry the same question" is only honest if
    the failure says so. A revoked key is retryable after an operator acts; a question
    the model refused outright is a different matter entirely, and the field is where
    that distinction will live when a second failure kind exists.
    """

    code: ErrorCode
    detail: str
    retryable: bool = True


@dataclass(frozen=True, slots=True)
class AskOutcome:
    """What one question produced, terminal state included.

    `content` is the whole answer as it was written — for a refusal, the bilingual
    refusal text; for a failure, empty, because there is no answer to show and showing
    the partial text a broken call produced would be exactly the silent fallback the
    ticket forbids.

    The `message_id` is the row id, generated before the first byte is sent so that
    `start` can carry it and a client can link the answer before it finishes arriving.
    """

    message_id: UUID
    content: str = ""
    citations: tuple[Citation, ...] = ()
    refusal: bool = False
    language: AnswerLanguage = AnswerLanguage.ES
    model: str | None = None
    provider: str | None = None
    token_in: int = 0
    token_out: int = 0
    latency_ms: int = 0
    failure: AskFailure | None = None

    @property
    def answered(self) -> bool:
        """True when the model produced the answer, which is what `token_out > 0` means."""
        return not self.refusal and self.failure is None


@dataclass(frozen=True, slots=True)
class AnswerEvent:
    """One thing that happened while answering, in the order it happened.

    A single shape for all six kinds (see `EventKind`) so the route's translation to SSE
    is one branch per kind and the driver's tests are a list comparison. `data` is the
    payload; `text` is the increment for a `delta` and empty otherwise, so a streaming
    loop does not have to reach into a payload dict to write the first byte.
    """

    kind: EventKind
    data: dict[str, Any] = field(default_factory=dict)
    text: str = ""


@dataclass(frozen=True, slots=True)
class ConversationRef:
    """The conversation a message belongs to, as the route needs to name it."""

    id: UUID
    user_id: UUID
    title: str


def title_for(question: str) -> str:
    """A conversation's title: the question, shortened on a word where one is available.

    Derived from the question rather than asked for, because §3.6's `title` exists so a
    list of a person's own conversations is readable, and a client that had to name a
    conversation before asking its first question would be asking for something nobody
    has an opinion about yet. `Message 90-day retention` means the title outlives the
    screen that created it, so it is stored rather than recomputed.
    """
    cleaned = " ".join(question.split())
    if len(cleaned) <= TITLE_CHARS:
        return cleaned or "?"
    clipped = cleaned[:TITLE_CHARS]
    boundary = clipped.rfind(" ")
    if boundary > TITLE_CHARS // 2:
        clipped = clipped[:boundary]
    return clipped


def new_id() -> UUID:
    """A uuid4, named so the two places that generate one for a message are greppable."""
    return uuid4()


__all__ = [
    "DEFAULT_ANSWER_LANGUAGE",
    "TITLE_CHARS",
    "AnswerEvent",
    "AnswerLanguage",
    "AskFailure",
    "AskOutcome",
    "Citation",
    "CitationDebug",
    "ConversationRef",
    "EventKind",
    "RetrievalDebug",
    "citations_json",
    "new_id",
    "title_for",
]
