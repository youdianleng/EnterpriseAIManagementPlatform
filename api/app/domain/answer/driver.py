"""One question in, one streamed, cited, persisted answer out.

This is §5.2's pipeline as one async generator, and the order of its steps *is* the
ticket's behaviour:

    1. filter the retrieval to what the caller may read — `answer_filter_for`
    2. hybrid search over that reach (ticket 33's service, unchanged)
    3. **insufficient evidence ⇒ refuse, and never construct the model call** (D20)
    4. otherwise: write the message row, build the prompt from the passages,
       stream the model
    5. close the message row with the citations, the accounting and the outcome

**Step 2 is also where §5.2's personal-document marker is decided** (ticket 36). The
retrieval clause recalls a personal document for its owner and for nobody else, so the
only personal citations an answer can carry are the asker's own — and
`SourceNotice.of(citations)` says whether any of them is one. The marker travels on the
`citations` event, before any text, and is stored on the message, so a client renders the
banner at the top without inferring it from the citation flags.

Six decisions a reader should have in mind, because each of them is a rule:

* **The refusal is decided before the model is touched.** `search()`'s
  `insufficient_evidence` is a state on a successful outcome, and the `return` on that
  branch happens *above* the call. That is what makes 「不调用生成模型」 a structural fact
  rather than a promise: `tests/test_answer.py` proves it with an adapter that records
  every call and would fail if the refusal path built one.

* **The permission filter is not optional here, and the module that produces it is ticket
  35's shared helper.** `domain/retrieval/filtering.py::answer_filter_for(principal)` is
  the principal → `FilterSpec` translation, and this driver calls it once, before the
  search. §4.3's constraint is that the condition is *pre*-retrieval: a passage the caller
  may not read must never be ranked, because a ranked passage is already part of the
  answer. An answer grounded in an unreachable document would leak it twice — once in the
  text and once in the citation, which names the file.

* **The row exists before the first byte, and the transaction does not end until the
  stream does.** The message is opened before the model is called — so a crash mid-answer
  leaves an attempt behind and the `start` event can carry an id a client can link — and
  completed at the end. What must *not* happen in between is a commit, because the
  permission context `deps.current_principal` publishes is `set_config(…, is_local => true)`
  and therefore transaction-scoped: committing after the INSERT silently retired it, and
  the retrieval then ran with no context and returned nothing from every document. The
  single commit is in `_finish`, and `stream()`'s own comment records the symptom.

* **A model failure is an outcome, not an exception.** By the time the model is being read
  the SSE response has already sent its `start`, so no status code can change; the driver
  catches `AnswerModelUnavailable`, records `ERR_ANS_001` on the row and yields the
  `error` event. It never yields the partial text: half an answer presented as a whole one
  is precisely the silent failure the ticket names. The retry entry point is the same
  request again — the question is in the client's hands, and the id of the attempt that
  failed is in the stream's `start`.

* **The latency is measured around the model, not around the request.** `latency_ms` is
  §5.2's 「耗时」 for the generation step, which is the number that tells an operator
  whether the provider is slow; the whole-request time is what the access log already
  records, and mixing the two would make the column mean "how big was the question".

* **The question is audited, and separately from the transcript.** `rag_messages` is
  D18's 90-day material with a reader (its owner) and a second reader (compliance);
  §3.7's four-year trail is a record of *acts*. So `conversation.asked` is written with
  the outcome — answered, refused, or failed, and which documents grounded it — and no
  answer text, because the trail's job is to say what happened rather than to become a
  second copy of the conversation that outlives its retention.

* **The token counts come from the vendored `cl100k_base` counter.** `token_in` is the
  assembled prompt and `token_out` the answer as it was written — the same counter ticket
  32 vendored for chunk sizes, reused rather than adding a second dependency, and *the
  real count of the text this process sent and received* rather than the provider's
  billing figure (which no streaming response carries without a second request).
"""

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from app.audit import AuditAction, record
from app.core.errors import ErrorCode, definition_of
from app.domain.access.principal import Principal
from app.domain.answer.chat import AnswerModelUnavailable, ChatModel
from app.domain.answer.models import (
    AnswerEvent,
    AnswerLanguage,
    AskFailure,
    AskOutcome,
    Citation,
    CitationDebug,
    EventKind,
    RetrievalDebug,
    SourceNotice,
    citations_json,
)
from app.domain.answer.prompts import (
    REFUSAL_MESSAGE_KEY,
    prompt_messages,
    refusal_text,
)
from app.domain.answer.repository import ConversationNotFound, PostgresAnswerRepository
from app.domain.document.tokenizer import count_tokens
from app.domain.retrieval.filtering import answer_filter_for, retrieval_filter_explanation
from app.domain.retrieval.models import DEFAULT_LIMIT, SearchOutcome
from app.domain.retrieval.service import RetrievalService
from app.logging import get_logger

logger = get_logger(__name__)

#: Spanish function words and characters, used to read a question's language. A list
#: rather than a library: §5.2 names three languages for the *input* (西/英/中) and only
#: two for the interface, so the decision this feeds is "which of two prompt sentences",
#: and a detector is not needed to choose between them. A question with none of these
#: markers and none of the accented characters is answered as English and the language is
#: recorded as `other` if it is neither — the prompt then says "the language of the
#: question", which is the honest instruction when the answer is in Chinese.
_SPANISH_MARKERS = frozenset(
    {
        "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "que",
        "cuantos", "cuantas", "cuanto", "cuanta", "como", "cual", "cuales", "donde",
        "cuando", "por", "para", "con", "sin", "es", "son", "esta", "estan", "hay",
        "puedo", "puede", "tengo", "tiene", "dias", "permiso", "vacaciones", "politica",
        "empresa", "personal", "solicitud", "horas", "trabajo", "y", "o", "no", "si",
        "se", "su", "sus", "mi", "mis", "al", "en", "lo", "le", "me",
    }
)

_SPANISH_CHARS = "áéíóúñ¿¡ü"


def detect_language(question: str) -> AnswerLanguage:
    """Which of the two prompts the question is written in. See `_SPANISH_MARKERS`.

    Deliberately a marker count rather than a length comparison, because a Spanish
    question is usually longer than its English translation only by accident, and a
    heuristic that flips on length would flip on a single extra clause. Accents and the
    inverted question mark are the strongest signals and are counted first.
    """
    lowered = question.lower()
    accented = sum(1 for character in lowered if character in _SPANISH_CHARS)
    words = {word.strip("¿?¡!.,;:()\"'«»") for word in lowered.split()}
    spanish = len(words & _SPANISH_MARKERS)
    english = len(words & _ENGLISH_MARKERS)
    if accented or spanish > english:
        return AnswerLanguage.ES
    if english:
        return AnswerLanguage.EN
    # Neither language's markers: a question in Chinese, or a two-word identifier. The
    # prompt then says "the language of the question" and the answer follows it, which is
    # what §5.2 requires; the record says `other` rather than claiming one of the two.
    return AnswerLanguage.OTHER


#: The mirror of `_SPANISH_MARKERS`, and much shorter: "of", "the" and "how many" are
#: enough to separate an English question from a Spanish one, and a longer list would
#: start claiming questions that are simply short.
_ENGLISH_MARKERS = frozenset(
    {
        "the", "of", "is", "are", "how", "many", "what", "when", "where", "who", "which",
        "do", "does", "can", "i", "we", "my", "our", "for", "to", "in", "on", "and",
        "days", "leave", "policy", "company", "employee", "request", "hours", "work",
    }
)


class AnswerService:
    """The module. One public verb: `stream(question, principal)`."""

    def __init__(
        self,
        repository: PostgresAnswerRepository,
        retrieval: RetrievalService,
        model: ChatModel,
        *,
        session=None,  # noqa: ANN001 - AsyncSession; optional so a test can build one
        limit: int = DEFAULT_LIMIT,
    ) -> None:
        self._repository = repository
        self._retrieval = retrieval
        self._model = model
        self._session = session
        self._limit = limit

    @property
    def model(self) -> ChatModel:
        """The adapter in use, for the seam's test and for the route's `start` event."""
        return self._model

    async def stream(
        self,
        question: str,
        principal: Principal,
        *,
        conversation_id: UUID | None = None,
    ) -> AsyncIterator[AnswerEvent]:
        """§5.2, as events. See the module docstring for the order and why.

        The first event is `start` and the last is `done`; between them are either
        `citations` + one or more `delta`s, or a single `refusal`, or a single `error`.
        There is one exception, and it is the one case where no conversation exists to have
        started: a `conversation_id` that is not (or is no longer) the caller's yields a
        lone terminal `error` and nothing else. That is a narrower guarantee than ticket
        34's contract, recorded here rather than left to be discovered — and it is the
        stream's answer to a question `ask_route` normally answers with a 404 first.
        """
        cleaned = question.strip()
        # The conversation and the message row are written **in this transaction and not
        # committed yet**, which is a requirement rather than a preference.
        #
        # `deps.current_principal` publishes the permission context with
        # `set_config(..., is_local => true)`, which is *transaction*-scoped: a commit ends
        # it. Committing here — the obvious reading of "the row must exist before the first
        # byte" — therefore made the retrieval below run with no context at all, and every
        # document row-level policy answered "no context, no rows". The symptom is a
        # knowledge base that behaves as if it were empty: every question refused, with
        # `best_score: 0.0`. It was found by comparing a service built on the platform's
        # connection (which never publishes a context and so never loses one) against the
        # running endpoint.
        #
        # So there is exactly **one** commit on this path, at the end, in `_finish`. The row
        # is still created before the model is called and before the first event is
        # produced, which is what makes a crash mid-answer leave an attempt behind; what it
        # is not is durable to *other* connections before the stream ends, and that is the
        # price of an RLS context that survives the retrieval.
        try:
            conversation = await self._repository.ensure_conversation(
                user_id=principal.user_id, question=cleaned, conversation_id=conversation_id
            )
        except ConversationNotFound:
            # **The race, and the only place it can be answered.** `ask_route` checks the
            # same ownership before it builds this generator, so the ordinary "not yours,
            # or deleted" answer is a 404 with a status code. This is the window in
            # between: the conversation is deleted while the request is already in flight —
            # and by then the 200 has been sent, so the honest answer is the stream's own
            # terminal frame rather than an exception raised inside a body iterator, which
            # surfaces as a failed response with no explanation at all. Ticket 37 made this
            # reachable for the first time: the owner can delete a conversation from a
            # second tab while the first one is asking a follow-up.
            yield AnswerEvent(
                kind=EventKind.ERROR,
                data={
                    # There is no message row: the conversation was refused before one was
                    # opened, and inventing an id would name a row nobody can look up.
                    "message_id": None,
                    "conversation_id": str(conversation_id),
                    "code": ErrorCode.NOT_FOUND.value,
                    "message_key": definition_of(ErrorCode.NOT_FOUND).message_key,
                    "retryable": False,
                },
            )
            return
        message_id = await self._repository.open_message(
            conversation_id=conversation, question=cleaned
        )

        language = detect_language(cleaned)
        yield AnswerEvent(
            kind=EventKind.START,
            data={
                "message_id": str(message_id),
                "conversation_id": str(conversation),
                "question": cleaned,
                "model": self._model.name,
                "provider": self._model.provider,
                "language": str(language),
            },
        )

        # --- 1 & 2: the filtered search ------------------------------------
        spec = answer_filter_for(principal)
        outcome = await self._retrieval.search(
            cleaned, filter_spec=spec, limit=self._limit
        )
        citations = tuple(Citation.of(hit) for hit in outcome.hits)
        # §5.2/Q29's marker, decided once from the citations that will actually ground
        # the answer (ticket 36). Computed *before* the refusal branch below so the two
        # impossible states cannot be built: a refusal carries no citations and therefore
        # no marker, and an answer carries the marker exactly when one of its citations
        # is somebody's personal upload. See `SourceNotice.of`.
        notice = SourceNotice.of(citations)
        debug = self._debug(outcome, spec)

        # --- 3: D20, and the model is not built into a call ----------------
        if outcome.insufficient_evidence:
            async for event in self._refuse(
                message_id=message_id,
                conversation=conversation,
                language=language,
                debug=debug,
            ):
                yield event
            return

        # --- 4: the prompt, and the model ----------------------------------
        # The marker travels **before any text**, with the citations it labels: a client
        # that renders the label "at the top of the answer" (§5.2's 回答顶部) has to know
        # about it while the first tokens are still being generated, and a marker that
        # only arrived on `done` would be a banner the reader sees after reading.
        yield AnswerEvent(
            kind=EventKind.CITATIONS,
            data={
                "citations": _citation_payloads(citations),
                "source_notice": notice.as_json() if notice is not None else None,
            },
        )

        messages = prompt_messages(cleaned, citations, language)
        started = time.perf_counter()
        text: list[str] = []
        failure: AskFailure | None = None
        try:
            async for increment in self._model.stream(messages):
                if not increment:
                    continue
                text.append(increment)
                yield AnswerEvent(kind=EventKind.DELTA, text=increment)
        except AnswerModelUnavailable as error:
            failure = AskFailure(code=error.code, detail=error.detail)
            # The answer so far is discarded rather than completed: an answer that stops
            # mid-sentence is not an answer, and the ticket's rule is that a failure is
            # explicit rather than a degraded one. The row records the code so an operator
            # can see the attempt, and the client gets the retry entry point.
            #
            # Ticket 42: the *last* attempt is what a reader needs. Before the chain existed
            # there was one provider and its name was the model's; with one, `provider` is
            # the adapter that failed last, which is the one whose error this is.
            logger.warning(
                "answer_model_unavailable",
                message_id=str(message_id),
                model=_attempted(self._model).model,
                provider=_attempted(self._model).provider,
                detail=error.detail,
            )
        # Only the success path counts its latency: a timed-out call has no meaningful
        # generation time, and recording it would make a 60-second timeout look like a
        # 60-second answer.
        latency_ms = int((time.perf_counter() - started) * 1000) if failure is None else 0

        # **Which provider actually answered** (ticket 42). Read from the attempt that ran
        # rather than from `self._model.provider`, because a chain's own property reports the
        # *primary* before it has been called — the value the `start` event above carries —
        # and the row must say who answered. For a single adapter the two are the same value,
        # which is why nothing else about this method changes.
        answered = _attempted(self._model)
        if failure is None:
            await self._record_fallbacks(message_id, conversation, answered)

        # --- 5: close the row ----------------------------------------------
        outcome_value = AskOutcome(
            message_id=message_id,
            content="" if failure is not None else "".join(text),
            citations=() if failure is not None else citations,
            refusal=False,
            language=language,
            model=None if failure is not None else answered.model,
            provider=None if failure is not None else answered.provider,
            token_in=0 if failure is not None else count_tokens(_prompt_text(messages)),
            token_out=0 if failure is not None else count_tokens("".join(text)),
            latency_ms=latency_ms,
            failure=failure,
            # Dropped with the citations on a failure: a message with no answer and no
            # sources has nothing to label, and a stored marker on it would say the
            # answer quoted a personal document when no answer was produced at all.
            source_notice=None if failure is not None else notice,
        )
        await self._finish(message_id, conversation, outcome_value, debug)

        if failure is not None:
            yield AnswerEvent(
                kind=EventKind.ERROR,
                data={
                    "message_id": str(message_id),
                    "conversation_id": str(conversation),
                    "code": failure.code.value,
                    "message_key": _message_key(failure.code),
                    "retryable": failure.retryable,
                },
            )
            return

        yield AnswerEvent(
            kind=EventKind.DONE,
            data={
                "message_id": str(message_id),
                "conversation_id": str(conversation),
                "citations": _citation_payloads(citations),
                # Repeated here as well as on `citations`, for the reason the citations
                # are: `done` is the record of what was stored, and a client that joined
                # a stream late — or that reads the message back later — finds the
                # marker in the same place as everything else it renders.
                "source_notice": notice.as_json() if notice is not None else None,
                # The provider that *answered*, not the one that was tried first: a client
                # showing "answered by X" has to show the same X the row stored, or the
                # stream and the transcript disagree (ticket 42).
                "model": answered.model,
                "provider": answered.provider,
                "token_in": outcome_value.token_in,
                "token_out": outcome_value.token_out,
                "latency_ms": latency_ms,
                "is_refusal": False,
            },
        )

    # --- the two terminal branches ------------------------------------------

    async def _refuse(
        self,
        *,
        message_id: UUID,
        conversation: UUID,
        language: AnswerLanguage,
        debug: RetrievalDebug,
    ) -> AsyncIterator[AnswerEvent]:
        """D20: the knowledge base holds no basis, said in both languages, with no model.

        The text is the constant in `prompts.py` rather than a generation, and the
        language is carried for the record rather than used to choose one — §5.2 asks for
        a bilingual statement, and the catalogue key is what a client renders if it wants
        exactly one.

        The citations are empty *and that is asserted*: a refusal that carried citations
        would be self-contradictory, and `hits` is empty exactly when
        `insufficient_evidence` is true, which is what `SearchOutcome`'s own docstring
        promises.
        """
        content = refusal_text()
        value = AskOutcome(
            message_id=message_id,
            content=content,
            citations=(),
            refusal=True,
            language=language,
            # No model ran, so no model is recorded. Writing the configured model's name
            # would say a model answered a question it was never asked.
            model=None,
            provider=None,
            token_in=0,
            token_out=0,
            latency_ms=0,
        )
        await self._finish(message_id, conversation, value, debug)
        yield AnswerEvent(
            kind=EventKind.REFUSAL,
            data={
                "message_id": str(message_id),
                "conversation_id": str(conversation),
                "content": content,
                "message_key": REFUSAL_MESSAGE_KEY,
                "best_score": debug.best_score,
                "threshold": debug.threshold,
                "is_refusal": True,
                "model_called": False,
                # Stated rather than omitted (ticket 36). A refusal has no citations, so it
                # has nothing to label; an explicit `null` is what stops a client from
                # having to decide whether an absent key means "no marker" or "the server
                # did not say" — the same reason `done` carries an empty citations list
                # rather than nothing at all.
                "source_notice": None,
            },
        )
        yield AnswerEvent(
            kind=EventKind.DONE,
            data={
                "message_id": str(message_id),
                "conversation_id": str(conversation),
                "citations": [],
                "source_notice": None,
                "model": None,
                "provider": None,
                "token_in": 0,
                "token_out": 0,
                "latency_ms": 0,
                "is_refusal": True,
            },
        )

    # --- internals ----------------------------------------------------------

    def _debug(self, outcome: SearchOutcome, spec) -> RetrievalDebug:  # noqa: ANN001
        """The retrieval half of the message, as the JSONB column holds it.

        `filter_explanation` is rendered from the *same* spec the search was given, so a
        stored message says which predicate grounded it rather than which one the code
        intended — §4.3's 「便于人工复核」, one row at a time.
        """
        return RetrievalDebug(
            query=outcome.query,
            filtered=outcome.filtered,
            filter_explanation=retrieval_filter_explanation(spec),
            legs_used=tuple(str(leg) for leg in outcome.legs_used),
            embedder=outcome.embedder,
            threshold=outcome.threshold,
            best_score=outcome.best_score,
            hits=tuple(CitationDebug.of(hit) for hit in outcome.hits),
        )

    async def _finish(
        self,
        message_id: UUID,
        conversation: UUID,
        outcome: AskOutcome,
        debug: RetrievalDebug,
    ) -> None:
        """Complete the row, touch the conversation, audit, commit.

        One place for all three terminal states, so a refusal and a failure cannot diverge
        in what they record. The completion's `False` — another request already finished
        this message — is logged rather than raised: the caller's stream has already been
        answered, and the row it raced is the one the database should keep.

        The permission condition comes off `debug` rather than being rendered again: the
        trace already holds the sentence the run applied, and rendering it a second time
        for the `retrieval_filter` column is how the two could disagree.
        """
        written = await self._repository.complete_message(
            message_id,
            outcome=outcome,
            debug=debug,
            filter_explanation=debug.filter_explanation or "",
        )
        if not written:  # pragma: no cover - only reachable through a concurrent retry
            logger.warning("answer_message_already_completed", message_id=str(message_id))
        await self._repository.touch_conversation(conversation)
        await self._audit(message_id, conversation, outcome, debug)
        await self._commit()

    async def _audit(
        self,
        message_id: UUID,
        conversation: UUID,
        outcome: AskOutcome,
        debug: RetrievalDebug,
    ) -> None:
        """The four-year trail's record of one question: what was asked, and what happened.

        Written **in the same transaction as the completion**, so a question is either
        answered and audited or neither. The alternative — a separate commit — would let a
        process die between them and leave an answer nobody can account for, which is the
        one thing an audit trail exists to prevent.

        No answer text and no quote is stored. The trail says *that* the knowledge base was
        asked, which documents grounded the answer, and whether it refused; the text itself
        lives in `rag_messages` under D18's retention, and copying it here would make the
        four-year trail a four-year transcript, which is a different design decision and
        not one this ticket may take.

        **Ticket 42's fallback entry is written beside it, from the same facts.** One
        `conversation.asked` entry per question, with `provider`/`model` already carrying who
        answered; and when the chain moved on, one `answer.provider_fallback` entry naming
        the providers and the technical kinds, because "which provider served this and how
        often does it fail over" is a question about the *act*, and the row alone cannot
        answer how many attempts it took. Neither carries text: the fields are provider
        names, model names, run-local counts and a `TECHNICAL_FAILURES` kind.
        """
        if self._session is None:  # pragma: no cover - the route always passes one
            return
        await record(
            self._session,
            action=AuditAction.CONVERSATION_ASKED,
            entity_type="rag_conversation",
            entity_id=conversation,
            after={
                "message_id": str(message_id),
                "refusal": outcome.refusal,
                "error_key": (
                    outcome.failure.code.value if outcome.failure is not None else None
                ),
                "model": outcome.model,
                "provider": outcome.provider,
                "attempts": len(_attempts(self._model)) or 1,
                "grounding_documents": sorted(
                    {str(hit.document_id) for hit in debug.hits}
                ),
                "hits": len(debug.hits),
                "best_score": debug.best_score,
                "threshold": debug.threshold,
            },
            reason=(
                "no basis in the knowledge base (D20)"
                if outcome.refusal
                else (
                    f"the model failed: {outcome.failure.code.value}"
                    if outcome.failure is not None
                    else "answered from the retrieved passages"
                )
            ),
        )

    async def _record_fallbacks(
        self, message_id: UUID, conversation: UUID, answered: "_Attempt"
    ) -> None:
        """Log and audit a degradation, once, when the chain moved on.

        **Two records, and neither carries conversation text.** The structured log line is
        the operator's ("the primary failed at 09:14 with a timeout"); the audit entry is the
        trail's, keyed on the conversation, so "how often does this installation fall back"
        is a query against `audit_log` rather than a search through fourteen days of logs.
        DESIGN D24's own rules apply to the entry: an action from the catalogue, a before and
        after that survive JSONB, `initiated_by="system"` because no person acted.

        The `entity_id` is the conversation, not the message: §3.6 keys a conversation's
        trail on the conversation, and a reader asking "what happened to this thread" is the
        reader this entry is for.
        """
        failed = [attempt for attempt in _attempts(self._model) if attempt.outcome != "ok"]
        if not failed:
            return

        logger.warning(
            "chat_provider_fallback",
            message_id=str(message_id),
            primary=failed[0].provider,
            failures=[attempt.failure for attempt in failed],
            providers_tried=[attempt.provider for attempt in failed],
            provider_used=answered.provider,
            model_used=answered.model,
            # A count, never a question: the same discipline `records.py` applies to a trace.
            attempts=len(_attempts(self._model)),
        )
        if self._session is None:  # pragma: no cover - the route always passes one
            return
        await record(
            self._session,
            action=AuditAction.ANSWER_PROVIDER_FALLBACK,
            entity_type="rag_conversation",
            entity_id=conversation,
            before={"provider": failed[0].provider, "model": failed[0].model},
            after={
                "message_id": str(message_id),
                "provider_used": answered.provider,
                "model_used": answered.model,
                "failures": [attempt.failure for attempt in failed],
                "providers_tried": [attempt.provider for attempt in failed],
                "attempts": len(_attempts(self._model)),
            },
            reason=(
                "the primary provider failed technically ("
                + ", ".join(attempt.failure for attempt in failed)
                + ") and the configured chain moved on (§5.3/D17); no conversation text is "
                "recorded here"
            ),
            initiated_by="system",
        )

    async def _commit(self) -> None:
        await self._repository.commit()


# --- reading the provider accounting back -------------------------------------


@dataclass(frozen=True, slots=True)
class _Attempt:
    """One provider's turn: names and an outcome, never text. See `chat.ChatAttempt`.

    A private mirror rather than an import of `chat.ChatAttempt`, and for a reason worth
    stating: the driver's dependency is the `ChatModel` *protocol*, and a driver that
    imported the chain's dataclass would be a driver that could not be given a second
    implementation of the protocol. The protocol itself carries no `attempts` attribute — a
    single adapter has nothing to report — so this is the shape a model *may* expose.
    """

    provider: str = ""
    model: str = ""
    outcome: str = "ok"
    failure: str = ""


def _attempts(model: object) -> tuple[_Attempt, ...]:
    """What the model recorded about its own calls. Empty for a model that records nothing."""
    raw = getattr(model, "attempts", None)
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(
        item if isinstance(item, _Attempt) else _Attempt(**_fields_of(item))
        for item in raw
    )


def _fields_of(item: object) -> dict[str, str]:
    """The four fields this module reads off an attempt, whatever class produced it."""
    return {
        name: str(getattr(item, name, ""))
        for name in ("provider", "model", "outcome", "failure")
    }


def _attempted(model: object) -> _Attempt:
    """The provider that answered, or the last one tried. **What the row must record.**

    A success is the last attempt when the chain moved on (`provider_used` is the fallback,
    not the primary) and the only attempt otherwise. A failure is the last provider tried,
    because that is the error the client is being told about; `provider_used` and
    `model_used` are `None` on that row regardless, so nothing claims it answered.
    """
    attempts = _attempts(model)
    return attempts[-1] if attempts else _Attempt(provider=model.provider, model=model.name)


def _prompt_text(messages: list[dict]) -> str:
    """Every message's content, joined: what `token_in` counts.

    The whole prompt is counted rather than the passages alone, because the token count
    exists to explain what the request cost the provider — the system prompt is most of
    the fixed part and leaving it out would under-report every call by the same amount.
    """
    return "\n".join(str(message.get("content", "")) for message in messages)


def _citation_payloads(citations: tuple[Citation, ...]) -> list[dict]:
    """The citation list as the SSE payload and the JSONB column both hold it.

    One function for both, so the stream a client renders and the row an auditor reads
    cannot disagree about what a citation said.
    """
    return citations_json(citations)


def _message_key(code: ErrorCode) -> str:
    return definition_of(code).message_key


__all__ = ["AnswerService", "detect_language"]
