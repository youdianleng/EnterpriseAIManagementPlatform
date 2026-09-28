"""Turning a grounded prompt into answer text, incrementally.

`docs/architecture/codebase-design.md` §4 refuses a seam around the *embedding model* and
this module is the opposite case, which is why it mirrors
`domain/document/embeddings.py`'s shape rather than inventing one:

* **The generation model genuinely is interchangeable.** §5.3's `CHAT_CHAIN` lists three
  providers — "openai:gpt-4o, deepseek:deepseek-chat, anthropic:claude-*" — and the design
  says degradation between them is fine on a technical failure and never on a change of
  answer quality. So the interface is a real interface: several adapters, one of which is
  the production one, and `name` written on the message row is what keeps the choice
  auditable after the fact (`rag_messages.model_used`, §5.3's own requirement).
* **Streaming is the interface, not an optimisation.** §5.2 is 「流式 SSE」 and the ticket
  puts the first token on screen in under 2.5 seconds, so a `complete()` that returned a
  string would make the requirement unimplementable behind this seam. `stream()` yields
  text increments and that is the only verb.
* **The real implementation is present, not a stub.** `OpenAIChatModel` posts to
  `/chat/completions` with `stream: true` and reads the provider's SSE frames; the only
  thing between this repository and a live call is a key. It is written with `urllib` on a
  worker thread, exactly as the embedder is, because a container needs no HTTP client for
  two endpoints and because blocking the event loop for the length of a generation would
  stall every other request this process is serving.
* **`StreamedChatModel` is the development and test adapter**, and it is honest about
  being one: it answers by *quoting the retrieved passages* it was given, never from its
  own knowledge — which is the one behaviour D20 requires of every adapter and the one a
  `random` fake could not have. Its `name` says what it is, and `CHAT_PROVIDER` has to be
  set to `fake` explicitly outside development, so a deployment that forgot a key gets
  `ERR_ANS_001` rather than a corpus-shaped answer written by a stand-in.

**Why a failure is one exception and not three.** `AnswerModelUnavailable` covers "no
key", "revoked", "rate limited", "unreachable" and "timed out", because the pipeline's
handling of all five is identical and the ticket forbids any of them from becoming an
answer: the stream closes with `ERR_ANS_001` and the message row records the code. What
the operator needs to tell them apart is in `detail`, which goes to the log and not to the
client.

**Why `stream()` is an async generator rather than returning an iterator.** The adapter
that does I/O must be able to `await` inside the loop, and the adapter that does not wants
to be able to yield without pretending. An async generator is the one shape both can
implement.
"""

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterable, Mapping
from typing import Protocol

from app.core.errors import ErrorCode

#: The OpenAI chat completions endpoint. A default rather than a setting for the reason
#: `embeddings.DEFAULT_BASE_URL` is one: a different endpoint is a different provider,
#: which is a change to this module rather than to a deployment's environment.
DEFAULT_BASE_URL = "https://api.openai.com/v1"

#: Seconds before a generation call is abandoned. The ticket's 「模型调用失败或超时」 is
#: this number expiring: the stream closes with `ERR_ANS_001`, never with the partial
#: text, because half an answer presented as a whole one is the silent failure the ticket
#: names.
REQUEST_TIMEOUT_SECONDS = 60.0

#: What the development adapter calls itself. Named so `rag_messages.model_used` can
#: never be mistaken for a model that exists, exactly as `FAKE_MODEL` is in
#: `domain/document/embeddings.py`.
FAKE_MODEL = "passage-quoting-v1"

#: The answer `StreamedChatModel` gives when the prompt carries no passage at all. It
#: should be unreachable — the driver refuses before it builds a prompt with no evidence —
#: and it is written as a sentence rather than raised because an adapter that crashed on
#: an empty prompt would turn a driver bug into a 500.
NO_PASSAGES = (
    "No passages were provided, so there is nothing in the knowledge base to answer from."
)


class AnswerModelUnavailable(Exception):
    """The answer could not be generated, for a reason an operator can act on.

    Carries the catalogue code rather than being one, for the reason
    `EmbeddingUnavailable` does: it is raised inside the transport and mapped to a stream
    event and to an audit-shaped record one level up. The `detail` is the provider's own
    message plus what it means, and it is deliberately *not* shown to the client — a
    provider's 401 body can name the key.
    """

    def __init__(
        self, detail: str, *, code: ErrorCode = ErrorCode.ANSWER_MODEL_UNAVAILABLE
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code


class ChatModel(Protocol):
    """The seam. One verb — stream an answer — and the model's name.

    `name` is not decoration: §5.3 requires the model that answered to be recorded, and a
    deployment that changed the model must be able to tell which messages were written by
    which. `provider` is the adapter's own name, recorded beside it so "we were on the
    fake in staging and the real one in production" is a query rather than a guess.
    """

    @property
    def name(self) -> str: ...

    @property
    def provider(self) -> str: ...

    def stream(self, messages: list[Mapping[str, str]]) -> AsyncIterator[str]: ...


class StreamedChatModel:
    """The development and test adapter: it quotes the passages it was given.

    **What it is.** It reads the `<passages>` block out of the user message, ranks the
    passages by how many of the question's terms they contain, and streams a short answer
    built from the best one — with the `[N]` markers the system prompt asks for. It is
    deterministic: the same question over the same corpus produces the same bytes, which
    is what lets a test assert a citation marker and a route test assert that text arrived
    in more than one increment.

    **What that buys.** The whole answer path — retrieval, the prompt, the streaming
    transport, the citations, the token accounting, the persistence — runs with no key and
    no network, and a test that asserts "the answer cites passage 1" is asserting about
    the *pipeline* rather than about a model's mood.

    **What it does not buy.** It cannot write Spanish prose, it cannot summarise, and it
    cannot be asked a question whose answer is not literally in a passage. It does not
    pretend to: it is named `passage-quoting-v1`, `CHAT_PROVIDER` has to say `fake`
    explicitly outside development, and `rag_messages.model_used` records this name so a
    message written by it is never mistaken for one a real model wrote.

    **It never answers from its own knowledge**, which is the one property every adapter
    must share: with no passage it says so instead of inventing a policy (D20).
    """

    def __init__(self, *, chunk_chars: int = 24) -> None:
        #: How much text one `delta` carries. Small enough that a test sees several
        #: increments and a client sees something appear immediately; an adapter that
        #: yielded the whole answer at once would make the streaming path untested.
        self._chunk_chars = chunk_chars

    @property
    def name(self) -> str:
        return FAKE_MODEL

    @property
    def provider(self) -> str:
        return "fake"

    async def stream(self, messages: list[Mapping[str, str]]) -> AsyncIterator[str]:
        answer = self.answer_for(messages)
        for start in range(0, len(answer), self._chunk_chars):
            yield answer[start : start + self._chunk_chars]

    def answer_for(self, messages: list[Mapping[str, str]]) -> str:
        """The whole answer, before it is cut into increments.

        Public so a test can assert the text without decoding the stream, and so the
        increments above are visibly the *same* text: a fake that streamed something
        other than what it would have answered would test a path no real adapter has.
        """
        question = _question_of(messages)
        passages = _passages_of(messages)
        if not passages:
            return NO_PASSAGES

        best_position, best_passage = _best_passage(question, passages)
        excerpt = _first_sentence(best_passage)
        return (
            f"The knowledge base answers this in passage [{best_position}]: "
            f"{excerpt} [{best_position}]"
        )


class OpenAIChatModel:
    """`POST /v1/chat/completions` with `stream: true`. The real implementation.

    **Present rather than absent, and the only thing missing is a key.** The request is
    the documented one — the model, the messages, `stream` — and the response is read as
    the provider sends it: `data: {json}` lines, each carrying a `choices[0].delta.content`
    increment, terminated by `data: [DONE]`. Anything else in the stream is ignored
    rather than guessed at: a provider that changes its framing should fail loudly, and a
    parser that "found no content" would answer the question with an empty string.

    **The call runs on a worker thread and the frames come back through a queue.** The
    blocking `urlopen` loop cannot live on the event loop, and the generator that the
    caller consumes has to be async — so the thread pushes increments and a sentinel onto
    a queue and the generator drains it. That is what keeps the first byte early: the
    queue hands each increment on as the socket delivers it, rather than the request
    returning one finished string.

    `timeout` bounds the *socket*, which is what makes a hung provider a failure rather
    than a request that never ends. A provider that keeps the connection open and sends
    nothing is the same case and is bounded by the same number.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key:
            raise AnswerModelUnavailable(
                "no OPENAI_API_KEY: the real chat model cannot be built without one"
            )
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    @property
    def name(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "openai"

    async def stream(self, messages: list[Mapping[str, str]]) -> AsyncIterator[str]:
        queue: asyncio.Queue[str | None | AnswerModelUnavailable] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def pump() -> None:
            """Read the provider's frames on this thread and post them to the queue."""
            try:
                for increment in self._frames(messages):
                    loop.call_soon_threadsafe(queue.put_nowait, increment)
            except AnswerModelUnavailable as error:
                loop.call_soon_threadsafe(queue.put_nowait, error)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        worker = loop.run_in_executor(None, pump)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, AnswerModelUnavailable):
                    raise item
                yield item
        finally:
            # The socket is closed by the worker's own context manager; awaiting the
            # executor here is what stops a cancelled client leaving a thread reading a
            # response nobody will consume.
            await worker

    # --- the blocking half --------------------------------------------------

    def _frames(self, messages: list[Mapping[str, str]]) -> Iterable[str]:
        """The provider's SSE frames, as the text increments they carry.

        The whole `urlopen` lifetime is inside this generator, so the socket is closed
        when the stream ends — including when the caller stops consuming, because the
        generator is closed by the thread's `for` loop ending or by the worker finishing.
        """
        body = json.dumps(
            {
                "model": self._model,
                "messages": list(messages),
                "stream": True,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                for raw in response:
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:") :].strip()
                    if payload == "[DONE]":
                        return
                    increment = _delta_of(payload)
                    if increment:
                        yield increment
        except urllib.error.HTTPError as error:
            raise AnswerModelUnavailable(self._http_detail(error)) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise AnswerModelUnavailable(
                f"the chat endpoint could not be reached: {error}"
            ) from error

    def _http_detail(self, error: urllib.error.HTTPError) -> str:
        """The provider's own message, shortened, plus what it means for the operator.

        The same five-way reading `embeddings._http_detail` makes, for the same reason: an
        operator should not have to know OpenAI's status codes to learn that a key has to
        be set, and 429 and timeout are the two §5.3 explicitly allows a chain to degrade
        on.
        """
        try:
            payload = json.loads(error.read().decode("utf-8"))
            message = payload.get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001 - a body that is not JSON tells us nothing
            message = ""
        if error.code in (401, 403):
            hint = "the API key is missing, revoked or not allowed for this model"
        elif error.code == 404:
            hint = f"the model {self._model!r} is not available to this key"
        elif error.code == 429:
            hint = "the account is rate limited or out of quota"
        elif error.code >= 500:
            hint = "the provider is failing; this is the case §5.3 lets a chain degrade on"
        else:
            hint = "the provider refused the request"
        return f"chat HTTP {error.code}: {hint}" + (f" ({message})" if message else "")


def _delta_of(payload: str) -> str:
    """One frame's text increment, or the empty string for a frame that carries none.

    `choices` may be empty (a usage-only frame or an error frame) and `delta` may hold
    only a role announcement, which is the first frame every OpenAI-compatible stream
    sends. Both are ordinary, and both must yield nothing rather than raise.
    """
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return ""
    if not isinstance(decoded, dict):
        return ""
    choices = decoded.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    delta = first.get("delta")
    if not isinstance(delta, dict):
        return ""
    content = delta.get("content")
    return content if isinstance(content, str) else ""


# --- reading the prompt back -------------------------------------------------


def _question_of(messages: list[Mapping[str, str]]) -> str:
    """The question, from the user message the prompt builder wrote."""
    for message in messages:
        if message.get("role") == "user":
            return _after(str(message.get("content", "")), "Question:")
    return ""


def _passages_of(messages: list[Mapping[str, str]]) -> list[str]:
    """The passage texts, in the order the prompt numbered them.

    Read from the delimited block rather than passed in a second argument, because the
    adapter must answer from *exactly what the model would see*: an adapter handed the
    passages separately could answer from a passage the prompt truncated away, and the
    fake would then be testing a pipeline no real model is in.
    """
    from app.domain.answer.prompts import PASSAGE_CLOSE, PASSAGE_OPEN

    for message in messages:
        if message.get("role") != "user":
            continue
        content = str(message.get("content", ""))
        start = content.find(PASSAGE_OPEN)
        end = content.find(PASSAGE_CLOSE)
        if start < 0 or end < 0:
            continue
        body = content[start + len(PASSAGE_OPEN) : end]
        return _blocks(body)
    return []


def _blocks(body: str) -> list[str]:
    """The passage bodies in `body`: text between a `[N] …` header and the next one.

    The header line carries the file name and the page, and it is dropped here because a
    quotation should be the passage and not its label. A passage truncated by
    `PASSAGE_PROMPT_CHARS` arrives with its own marker, so nothing has to be trimmed a
    second time.
    """
    blocks: list[str] = []
    current: list[str] = []
    for line in body.splitlines():
        if line.startswith("[") and "]" in line and line[1 : line.find("]")].isdigit():
            if current:
                joined = "\n".join(current).strip()
                if joined:
                    blocks.append(joined)
            current = []
            continue
        current.append(line)
    joined = "\n".join(current).strip()
    if joined:
        blocks.append(joined)
    return blocks


def _best_passage(question: str, passages: list[str]) -> tuple[int, str]:
    """The passage sharing the most terms with the question, and its 1-based number.

    Ties go to the earliest, so the answer is deterministic; the whole ranking is the
    retrieval stage's job and this is only enough of one for a stand-in to be useful.
    """
    from app.domain.retrieval.rerank import terms_of

    # A set, not the list `terms_of` returns: the list preserves order because proximity
    # matters to the real reranker, and nothing here is measuring position.
    wanted = set(terms_of(question))
    best_position = 1
    best_score = -1
    for position, passage in enumerate(passages, start=1):
        overlap = len(wanted & set(terms_of(passage)))
        if overlap > best_score:
            best_position, best_score = position, overlap
    return best_position, passages[best_position - 1]


def _first_sentence(passage: str) -> str:
    """The passage's first sentence, or the whole of it when it has no full stop."""
    cleaned = " ".join(passage.split())
    for stop in (". ", ".\n", "!"):
        index = cleaned.find(stop)
        if 0 <= index <= 400:
            return cleaned[: index + 1]
    return cleaned[:400]


def _after(content: str, marker: str) -> str:
    index = content.find(marker)
    if index < 0:
        return ""
    return content[index + len(marker) :].strip()


def build_chat_model(
    provider: str,
    *,
    api_key: str | None = None,
    model: str = "gpt-4o",
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> ChatModel:
    """The adapter a deployment asked for. **There is no `none`**, deliberately.

    `fake` is explicit, and the derivation in `config.Settings` gives development and test
    the fake and everything else `openai`, so a production without a key fails with
    `ERR_ANS_001` naming the key rather than answering from a stand-in. Unlike embeddings,
    there is no third answer: a corpus with no vectors still answers by full text, but an
    *answer* with no model has nothing to fall back to, and D20 forbids the one fallback
    that would look like one. So an unknown provider is refused here rather than
    degraded.
    """
    if provider == "fake":
        return StreamedChatModel()
    if provider == "openai":
        return OpenAIChatModel(api_key or "", model=model, base_url=base_url, timeout=timeout)
    raise AnswerModelUnavailable(
        f"unknown chat provider {provider!r}; expected one of fake, openai. There is no "
        "'none': an answer with no model would have to be written from the model's own "
        "knowledge, which is the fallback D20 forbids"
    )


__all__ = [
    "DEFAULT_BASE_URL",
    "FAKE_MODEL",
    "NO_PASSAGES",
    "REQUEST_TIMEOUT_SECONDS",
    "AnswerModelUnavailable",
    "ChatModel",
    "OpenAIChatModel",
    "StreamedChatModel",
    "build_chat_model",
]
