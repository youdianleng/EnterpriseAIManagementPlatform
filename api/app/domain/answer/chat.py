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

**Why this module also owns the provider chain (ticket 42).** §5.3's `CHAT_CHAIN` is
`[openai:gpt-4o, deepseek:deepseek-chat, anthropic:claude-*]`, and the chain is a property of
*the seam* rather than of any adapter: the rule that decides whether to move on (a technical
failure) and the record of what actually answered (which is what makes
`rag_messages.provider_used` honest) are the same rule and the same record whichever adapter
ran. So `stream_with_fallback` lives here, beside the adapters it composes, and
`domain/answer/driver.py` reads the result from the composed object rather than knowing how
many providers there were.

**`langchain-openai` was available and is deliberately not used.** The ticket allows either,
so the choice is recorded: extending this urllib adapter with a per-provider base URL and
model keeps one HTTP path in the repository — the one `ai/__init__.py` already maps
`ai/providers/` onto — instead of adding a second, larger dependency's request/response
machinery (its own retries, its own streaming decoder, its own callback layer) beside an
adapter that is already tested against the provider's real SSE frames. DeepSeek speaks the
OpenAI dialect, so it is `OpenAIProvider` with a different base URL and key — one adapter,
two configurations, which is the honest model of what the two providers are. Anthropic is a
different dialect and therefore has its own adapter below (`AnthropicChatModel`), because
the alternative — "configured but unimplemented" — is a chain entry that can never answer.
"""

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
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


#: **The technical failures, and the whole of what may cause a fallback** (§5.3/D17).
#:
#: The ticket's four — 「超时、限流、服务端错误、连接失败」 — plus the three an operator sees and
#: acts on, and each is a fact about the *call* rather than about the answer: a provider that
#: refused the key, a model this key may not use, an endpoint that is not there. What is
#: deliberately **absent** is any name for "the answer was poor", "the answer was empty" or
#: "the answer was short": there is no value in this tuple a chain could move on because of
#: a judgement about text, which is what makes 「绝不因为"回答质量下降"而自动切换」 a property of
#: the vocabulary rather than a rule somebody has to remember.
#:
#: A failure kind is a `str` and there is no enum: the value travels in a record and in a log
#: line, and an enum would make `records.py`'s `str(getattr(value, "value", value))` the
#: arbiter of what a trace says.
TECHNICAL_FAILURES: tuple[str, ...] = (
    "timeout",
    "rate_limit",
    "server_error",
    "connection_error",
    "authentication",
    "permission",
    "not_found",
    "protocol_error",
)

#: The `https://host/v1` root of a base URL, which is what a provider catalogue stores.
#: `api.openai.com` and `api.deepseek.com` are one spelling apart and a catalogue that had to
#: repeat `/v1` in every default would be a catalogue somebody eventually gets wrong once.
DEFAULT_API_VERSION = "v1"


class AnswerModelUnavailable(Exception):
    """The answer could not be generated, for a reason an operator can act on.

    Carries the catalogue code rather than being one, for the reason
    `EmbeddingUnavailable` does: it is raised inside the transport and mapped to a stream
    event and to an audit-shaped record one level up. The `detail` is the provider's own
    message plus what it means, and it is deliberately *not* shown to the client — a
    provider's 401 body can name the key.

    **`failure` is the technical kind** (ticket 42), and it is what makes degradation a
    decision rather than a catch-all: §5.3 lets a chain move on for a timeout, a 429, a 5xx
    or a connection error, and `stream_with_fallback` reads this field and **nothing else**
    to decide. A failure that is not in `TECHNICAL_FAILURES` would be a judgement about the
    answer, and no adapter in this repository raises this exception for one.
    """

    def __init__(
        self,
        detail: str,
        *,
        code: ErrorCode = ErrorCode.ANSWER_MODEL_UNAVAILABLE,
        failure: str = "",
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.failure = failure
        if failure and failure not in TECHNICAL_FAILURES:
            raise ValueError(
                f"{failure!r} is not a technical failure ({TECHNICAL_FAILURES}); a chain "
                "may only move on when the call failed, never when an answer looked weak"
            )



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
        provider: str = "openai",
        key_setting: str = "OPENAI_API_KEY",
    ) -> None:
        if not api_key:
            # A missing key is `authentication`, not a configuration error raised at import:
            # it is the case §5.3 lets a chain move on from, and it is *the* reason a chain
            # has a second entry at all. A single-adapter deployment still fails with
            # `ERR_ANS_001` naming the key — the exception travels the same path it always
            # did — but now the failure says *which kind* it is.
            raise AnswerModelUnavailable(
                f"no {key_setting}: the real chat model cannot be built without one",
                failure="authentication",
            )
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        #: Which provider this configuration *is*. `deepseek` speaks this dialect, so one
        #: adapter carries two configurations and this field is what keeps
        #: `rag_messages.provider_used` saying which of them answered.
        self._provider = provider

    @property
    def name(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return self._provider

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
            raise AnswerModelUnavailable(
                self._http_detail(error), failure=failure_kind_for(error.code)
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            # `TimeoutError` and a refused socket are different operator problems and the
            # same *decision* — move on — so they are different kinds of one failure rather
            # than two exception types. A `URLError` wrapping a timeout is the form the
            # socket actually raises, which is why the inner reason is read as well.
            raise AnswerModelUnavailable(
                f"the chat endpoint could not be reached: {error}",
                failure=failure_kind_for_os_error(error),
            ) from error

    def _http_detail(self, error: urllib.error.HTTPError) -> str:
        """The provider's own message, shortened, plus what it means for the operator.

        The same five-way reading `embeddings._http_detail` makes, for the same reason: an
        operator should not have to know OpenAI's status codes to learn that a key has to
        be set, and 429 and timeout are the two §5.3 explicitly allows a chain to degrade
        on. The message is the provider's; the meaning is `_http_hint`'s, which the Claude
        adapter reads too — one reading of a status code rather than two that drift.
        """
        try:
            payload = json.loads(error.read().decode("utf-8"))
            message = payload.get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001 - a body that is not JSON tells us nothing
            message = ""
        hint = _http_hint(error.code, self._model)
        return f"chat HTTP {error.code}: {hint}" + (f" ({message})" if message else "")


def failure_kind_for(status: int) -> str:
    """Which technical failure an HTTP status is. See `TECHNICAL_FAILURES`.

    The four the ticket names are all here and each maps to one name: 408 and 504 are
    timeouts, 429 is a rate limit, 5xx is a server error, and a 401/403/404 is the operator's
    to fix. Every branch is *technical* — there is deliberately no branch for a 200 whose
    body looked wrong, because a 200 whose body looked wrong is a protocol failure and is
    raised as one by the parser rather than classified here.
    """
    if status in (401, 403):
        return "authentication" if status == 401 else "permission"
    if status == 404:
        return "not_found"
    if status == 408 or status == 504:
        return "timeout"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "server_error"
    return "protocol_error"


def failure_kind_for_os_error(error: BaseException) -> str:
    """Whether a socket failure was the clock or the wire.

    `urllib` wraps a socket timeout in `URLError(reason=TimeoutError(...))`, so reading only
    the outer type would call every timeout a connection error — and a timeout and a refused
    connection are the two failures an operator debugs differently (a slow provider versus a
    wrong base URL). Both degrade, which is why they are one exception with two kinds.
    """
    reason = getattr(error, "reason", None)
    if isinstance(error, TimeoutError) or isinstance(reason, TimeoutError):
        return "timeout"
    if "timed out" in str(error).lower():
        return "timeout"
    return "connection_error"


class AnthropicChatModel:
    """`POST /v1/messages` with `stream: true`. Anthropic's dialect, and its own adapter.

    **Why this is not the OpenAI adapter with a flag.** The two APIs differ in the four things
    an adapter is: the path, the authentication header, which body key carries the system
    prompt, and which field of which frame carries the increment. A single class with a
    `dialect` parameter would be a class whose every method begins with a branch, and the
    branch that is wrong is the branch nobody exercises — which is exactly how a "supported"
    provider turns out not to be. §5.3 lists `anthropic:claude-*` as the third entry of
    `CHAT_CHAIN`, so this is the entry that closes the chain rather than a hypothetical.

    **The shape of the request is Anthropic's.** `system` is a top-level string rather than a
    message with `role: system` (Anthropic rejects that role), the messages are only `user`
    and `assistant`, `max_tokens` is required, and the response is an SSE stream of
    `content_block_delta` events whose `delta.text` is the increment. The event names are read
    and anything else is skipped, the same policy `_delta_of` follows: a provider that changes
    its framing should produce an empty stream that the driver reports rather than text
    assembled from frames this parser guessed at.
    """

    #: Large enough that a grounded answer is never cut off, and required by the API. It is a
    #: constant rather than a setting because a model that needs a longer answer is a prompt
    #: that needs shortening — §5.2 asks for a cited answer, not an essay.
    MAX_TOKENS = 2048

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        base_url: str = "https://api.anthropic.com",
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        api_version: str = "2023-06-01",
    ) -> None:
        if not api_key:
            raise AnswerModelUnavailable(
                "no ANTHROPIC_API_KEY: the Claude adapter cannot be built without one",
                failure="authentication",
            )
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._api_version = api_version

    @property
    def name(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "anthropic"

    async def stream(self, messages: list[Mapping[str, str]]) -> AsyncIterator[str]:
        queue: asyncio.Queue[str | None | AnswerModelUnavailable] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def pump() -> None:
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
            await worker

    def _frames(self, messages: list[Mapping[str, str]]) -> Iterable[str]:
        system, turns = _anthropic_turns(messages)
        body = json.dumps(
            {
                "model": self._model,
                "system": system,
                "messages": turns,
                "max_tokens": self.MAX_TOKENS,
                "stream": True,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/v1/messages",
            data=body,
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": self._api_version,
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
                    increment = _anthropic_delta(line[len("data:") :].strip())
                    if increment:
                        yield increment
        except urllib.error.HTTPError as error:
            raise AnswerModelUnavailable(
                f"claude HTTP {error.code}: {_http_hint(error.code, self._model)}",
                failure=failure_kind_for(error.code),
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise AnswerModelUnavailable(
                f"the claude endpoint could not be reached: {error}",
                failure=failure_kind_for_os_error(error),
            ) from error


def _anthropic_turns(
    messages: list[Mapping[str, str]],
) -> tuple[str, list[dict[str, str]]]:
    """The prompt as Anthropic wants it: a system string and user/assistant turns.

    The system prompt is `prompts.py`'s own, which is the same text the OpenAI adapter sends
    as a `system` message — so the two providers are asked the *same question*, which is what
    makes a fallback a fallback rather than a second product.
    """
    system_parts: list[str] = []
    turns: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = str(message.get("content", ""))
        if role == "system":
            system_parts.append(content)
        else:
            turns.append({"role": role if role in ("user", "assistant") else "user",
                          "content": content})
    return "\n\n".join(system_parts), turns


def _anthropic_delta(payload: str) -> str:
    """One Anthropic frame's text increment, or the empty string.

    Only `content_block_delta` carries text; `message_start`, `ping`, `content_block_stop`
    and `message_delta` are ordinary frames and must yield nothing rather than raise.
    """
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return ""
    if not isinstance(decoded, dict):
        return ""
    if decoded.get("type") != "content_block_delta":
        return ""
    delta = decoded.get("delta")
    if not isinstance(delta, dict):
        return ""
    text = delta.get("text")
    return text if isinstance(text, str) else ""


def _http_hint(status: int, model: str) -> str:
    """What a status means for the operator. One reading, two adapters, one sentence each."""
    if status in (401, 403):
        return "the API key is missing, revoked or not allowed for this model"
    if status == 404:
        return f"the model {model!r} is not available to this key"
    if status == 429:
        return "the account is rate limited or out of quota"
    if status >= 500:
        return "the provider is failing; this is the case §5.3 lets a chain degrade on"
    return "the provider refused the request"


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


# --- provider configuration and the chain (ticket 42) ------------------------


@dataclass(frozen=True, slots=True)
class KeylessChatModel:
    """The same dialect as `OpenAIChatModel`, with no `Authorization` header.

    A local runtime — Ollama, llama.cpp, vLLM — answers `/chat/completions` exactly as OpenAI
    does and does not check a key. Sending `Authorization: Bearer ` with an empty token is
    not "harmless": a gateway in front of the runtime may reject it, and a header that exists
    only to be empty is a header somebody later fills in with the wrong value. So the header
    is *absent*, which is the whole difference and the reason this is a class rather than a
    flag on the OpenAI adapter's body.
    """

    provider: str
    model: str
    base_url: str
    timeout: float = REQUEST_TIMEOUT_SECONDS

    @property
    def name(self) -> str:
        return self.model

    async def stream(self, messages: list[Mapping[str, str]]) -> AsyncIterator[str]:
        queue: asyncio.Queue[str | None | AnswerModelUnavailable] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def pump() -> None:
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
            await worker

    def _frames(self, messages: list[Mapping[str, str]]) -> Iterable[str]:
        body = json.dumps(
            {"model": self._model_or_default(), "messages": list(messages), "stream": True}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
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
            raise AnswerModelUnavailable(
                f"{self.provider} HTTP {error.code}: {_http_hint(error.code, self.model)}",
                failure=failure_kind_for(error.code),
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise AnswerModelUnavailable(
                f"the {self.provider} endpoint could not be reached: {error}",
                failure=failure_kind_for_os_error(error),
            ) from error

    def _model_or_default(self) -> str:
        return self.model or "llama3.1"


@dataclass(frozen=True, slots=True)
class UnavailableChatModel:
    """A configured provider this process holds no key for. **Present and failing.**

    A chain that *skipped* an unbuildable provider at construction would report
    `provider_used=deepseek` with nothing to say about why OpenAI was not tried — which is
    exactly the invisible degradation `rag_messages.provider_used` exists to make visible.
    So an adapter that cannot be built is replaced by this one, which reports the same
    `authentication` failure the constructor would have raised, at the moment the request
    tries it. The row then says "openai failed with authentication, deepseek answered", and
    an operator knows which variable to set.

    It streams nothing and raises before its first increment, which is the shape a
    connection failure has and the reason the chain's `yielded` guard is not involved.
    """

    provider: str
    model: str
    detail: str

    @property
    def name(self) -> str:
        return self.model

    async def stream(self, messages: list[Mapping[str, str]]) -> AsyncIterator[str]:
        raise AnswerModelUnavailable(self.detail, failure="authentication")
        yield ""  # pragma: no cover - unreachable, and present so this stays a generator


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """One entry of the chain: which provider, which model, and where to post.

    A dataclass rather than four parameters because the four travel together and because
    `build_chat_model`'s signature would otherwise grow a `provider` argument that is *also*
    the thing the other three describe. The defaults are §5.3's own list, one entry per
    provider, so a deployment that sets nothing gets the provider this repository defaults
    to (see `config.Settings.chat_provider_name`) and a deployment that sets `CHAT_PROVIDERS`
    gets exactly the values it named.
    """

    provider: str
    #: The model. `None` means the catalogue's default for this provider, which is the value
    #: §5.3 lists — a deployment that wants a different one names it, and the name is what
    #: `rag_messages.model_used` then records.
    model: str | None = None
    base_url: str | None = None
    #: The setting a missing key should be reported as, so the message names the variable an
    #: operator has to set rather than the one this file happens to read.
    key_setting: str | None = None
    #: Whether this provider **needs no key at all**. A keyless provider is usually a local
    #: runtime (Ollama, llama.cpp, vLLM) and a key-requiring one is a SaaS; the distinction is
    #: here rather than in a second catalogue because it is one fact about the entry and it is
    #: what decides whether a missing key is a failure or the ordinary configuration.
    anonymous: bool = False


#: **§5.3's `CHAT_CHAIN`, as a catalogue.** `openai` and `deepseek` are one dialect
#: (`OpenAIProvider`'s) with two configurations; `anthropic` is the other dialect. The
#: catalogue is a mapping rather than three branches so that `parse_provider_chain` can
#: name the providers it knows without a second list, and so that a typo in `CHAT_PROVIDERS`
#: is refused by name.
PROVIDERS: Mapping[str, ProviderConfig] = MappingProxyType(
    {
        "openai": ProviderConfig(
            provider="openai",
            model="gpt-4o",
            base_url="https://api.openai.com",
            key_setting="OPENAI_API_KEY",
        ),
        "deepseek": ProviderConfig(
            provider="deepseek",
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            key_setting="DEEPSEEK_API_KEY",
        ),
        "anthropic": ProviderConfig(
            provider="anthropic",
            model="claude-3-5-sonnet-latest",
            base_url="https://api.anthropic.com",
            key_setting="ANTHROPIC_API_KEY",
        ),
        # **The local, keyless entry**, and the reason it is in the catalogue rather than a
        # class of its own: `docker compose up` must work with no API key at all — that is the
        # verification standard this repository holds every ticket to — and §5.3's three
        # providers are all SaaS. Ollama speaks the OpenAI dialect, so it is the *same*
        # adapter with the `Authorization` header omitted, which is the only difference a
        # keyless endpoint has. Its base URL is `host.docker.internal`, the address a
        # container uses to reach a runtime on the host, because that is where somebody
        # running a local model has it.
        "ollama": ProviderConfig(
            provider="ollama",
            model="llama3.1",
            base_url="http://host.docker.internal:11434/v1",
            key_setting="OLLAMA_API_KEY",
            anonymous=True,
        ),
    }
)


def parse_provider_chain(configured: str | None, *, fallback: str) -> tuple[str, ...]:
    """`CHAT_PROVIDERS=openai,deepseek` → `("openai", "deepseek")`.

    **The chain is configuration, and this is the whole of the parsing.** Unset means one
    provider — the one `chat_provider_name` derives for the environment — which is what keeps
    `docker compose up` and every existing deployment behaving exactly as they did: a chain
    nobody configured is a chain of one, not a surprise second provider.

    **A name that is not in `PROVIDERS` is refused rather than dropped**, because a chain that
    silently lost an entry is a deployment that believes it has a fallback and does not. But
    **the fallback itself is always acceptable**, and that is not a loophole: `fallback` is
    the name this environment *derived for itself* — `fake` in development and test,
    `openai` elsewhere — so refusing it would mean a deployment could not name the adapter it
    is already running. `fake` is the case that makes this concrete: it is the in-process
    adapter and deliberately has no `PROVIDERS` entry (no endpoint, no model, no key), so the
    accepted set is the catalogue *plus* the derived name.
    """
    if configured is None or not configured.strip():
        return (fallback,)
    names = tuple(part.strip().lower() for part in configured.split(",") if part.strip())
    if not names:
        return (fallback,)
    allowed = set(PROVIDERS) | {fallback}
    unknown = [name for name in names if name not in allowed]
    if unknown:
        raise AnswerModelUnavailable(
            f"CHAT_PROVIDERS names {unknown}, which this deployment has no adapter for; "
            f"the adapters are {sorted(allowed)}"
        )
    # Ordered, and duplicates are kept: an operator who wrote `openai,openai` gets two
    # attempts at OpenAI, which is a legitimate retry and not a mistake this layer should
    # silently rewrite.
    return names


def build_chat_chain(
    names: Sequence[str],
    *,
    keys: Mapping[str, str | None],
    models: Mapping[str, str | None] | None = None,
    base_urls: Mapping[str, str | None] | None = None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> ChatModel:
    """The ranked adapters, wrapped in the one object the driver streams through.

    `names` is the configured order and `names[0]` is the primary. The adapters are built
    here, all of them, so a provider whose key is missing is *present and failing* rather
    than absent: the chain then records "openai tried, `authentication`" and moves on, which
    is the honest record of what happened. Skipping an unbuildable provider at construction
    would make `provider_used` say `deepseek` with no trace of why.

    The timeout is one number for the whole chain rather than one per provider, because it is
    a property of the *request* this process is serving (§9's first-token budget): two
    providers each allowed sixty seconds is a two-minute wait for a client who was promised
    two and a half seconds to the first token.
    """
    if not names:
        raise AnswerModelUnavailable(
            "the chat chain is empty; a chain with no provider is the 'none' this module "
            "refuses, because an answer with no model would have to come from the model's "
            "own knowledge (D20)"
        )
    models = models or {}
    base_urls = base_urls or {}
    adapters: list[ChatModel] = []
    for name in names:
        model = models.get(name) or _default_model(name)
        key = keys.get(name)
        config = PROVIDERS.get(name)
        if name != "fake" and not key and not (config and config.anonymous):
            # See `UnavailableChatModel`: a keyless SaaS provider is a chain entry that
            # fails, not an entry that is not there. A provider the catalogue marks
            # `anonymous` (a local runtime) has no key by design and is built below.
            setting = (
                config.key_setting if config and config.key_setting
                else f"{name.upper()}_API_KEY"
            )
            adapters.append(
                UnavailableChatModel(
                    provider=name,
                    model=model,
                    detail=(
                        f"no {setting}: the {name} adapter has no key in this deployment, "
                        f"so this is the attempt the chain moves on from"
                    ),
                )
            )
            continue
        if config is not None and config.anonymous:
            # No `Authorization` header at all: `KeylessChatModel`.
            adapters.append(
                KeylessChatModel(
                    provider=name,
                    model=model,
                    base_url=base_urls.get(name) or _default_base_url(name),
                    timeout=timeout,
                )
            )
            continue
        adapters.append(
            build_chat_model(
                name,
                api_key=key,
                # `fake` takes no base URL, no model name and no timeout, and is the one
                # adapter whose presence in a chain is explicit rather than configured:
                # `parse_provider_chain` never invents it.
                model=model,
                base_url=base_urls.get(name) or _default_base_url(name),
                timeout=timeout,
            )
        )
    return stream_with_fallback(adapters)


def _default_model(name: str) -> str:
    config = PROVIDERS.get(name)
    return (config.model if config else None) or ""


def _default_base_url(name: str) -> str:
    config = PROVIDERS.get(name)
    return (config.base_url if config else None) or DEFAULT_BASE_URL


@dataclass(frozen=True, slots=True)
class ChatAttempt:
    """One provider's turn at the request. The record that makes the accounting honest.

    `outcome` is `"ok"` or `"failed"` and `failure` is the technical kind for a failure —
    a member of `TECHNICAL_FAILURES`, never a judgement about the answer. Nothing else about
    the attempt is kept: not the prompt, not the text, which is what lets this object be
    logged and audited (see `driver.py`).
    """

    provider: str
    model: str
    outcome: str
    failure: str = ""


@dataclass(slots=True)
class FallbackChatModel:
    """A ranked list of adapters, presenting the `ChatModel` interface as their sum.

    **The degradation rule lives here and nowhere else.** A provider is abandoned exactly
    when `stream` raised `AnswerModelUnavailable`, which the `ChatModel` protocol's adapters
    raise only for a technical failure — and every one of those carries a
    `TECHNICAL_FAILURES` kind, which this class asserts. There is no branch on the text: an
    adapter that yielded a poor answer has *returned*, and a returned stream is the answer.

    **A stream that fails halfway is abandoned and its partial text is discarded**, and that
    is deliberate: the driver has already written those increments to a client, so the chain
    cannot un-write them — but it can refuse to append a second provider's text to a first
    provider's half-sentence, which would be a fabricated answer. So once a provider has
    yielded anything, its failure is re-raised; degradation only happens *before* the first
    increment. `tests/test_provider_chain.py` pins both halves.

    **`provider` and `name` report what answered.** Before any call they name the primary, so
    the `start` event a client receives says which provider is being tried first; after a
    stream ends they name the adapter that produced it, so the row the driver stores says
    which one actually did. That is the whole of 「每次请求记录实际使用的供应商与模型」.
    """

    adapters: tuple[ChatModel, ...]
    #: Every attempt this request made, in order. Read by the driver for the fallback log
    #: line and the audit entry; never carries text.
    attempts: list[ChatAttempt] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.adapters:
            raise ValueError(
                "a fallback chain needs at least one adapter; an empty one would answer "
                "with nothing"
            )

    @property
    def name(self) -> str:
        if self.attempts and self.attempts[-1].outcome == "ok":
            return self.attempts[-1].model
        return self.adapters[0].name

    @property
    def provider(self) -> str:
        if self.attempts and self.attempts[-1].outcome == "ok":
            return self.attempts[-1].provider
        return self.adapters[0].provider

    @property
    def provider_count(self) -> int:
        """How many providers were configured — a count, which is all a trace may say."""
        return len(self.adapters)

    def fallbacks(self) -> tuple[ChatAttempt, ...]:
        """The attempts that failed before the one that answered. Empty when the primary did."""
        return tuple(attempt for attempt in self.attempts if attempt.outcome != "ok")

    async def stream(self, messages: list[Mapping[str, str]]) -> AsyncIterator[str]:
        """Stream from the first provider that answers, in configuration order.

        The `raise` at the end is the all-providers-fail case, and it is the *last*
        provider's own exception with the chain's sentence in front of it: the driver maps it
        to `ERR_ANS_001`, the same explicit error one provider produced before this ticket,
        so 「明确错误而非静默空回答」 is the existing path rather than a second one.
        """
        failures: list[ChatAttempt] = []
        for adapter in self.adapters:
            yielded = False
            try:
                async for increment in adapter.stream(messages):
                    yielded = True
                    yield increment
            except AnswerModelUnavailable as error:
                kind = error.failure or "protocol_error"
                self._refuse_unknown_kind(kind, adapter)
                self.attempts.append(
                    ChatAttempt(
                        provider=adapter.provider,
                        model=adapter.name,
                        outcome="failed",
                        failure=kind,
                    )
                )
                if yielded:
                    # Half an answer is already on the wire; a second provider's text
                    # appended to it would be an answer nobody wrote.
                    raise
                failures.append(self.attempts[-1])
                continue
            self.attempts.append(
                ChatAttempt(provider=adapter.provider, model=adapter.name, outcome="ok")
            )
            return

        raise AnswerModelUnavailable(
            self._all_failed_detail(failures),
            failure=failures[-1].failure if failures else "protocol_error",
        )

    def _all_failed_detail(self, failures: Sequence[ChatAttempt]) -> str:
        """Every provider named, in order, with its failure kind. **No provider text.**"""
        chain = ", ".join(
            f"{attempt.provider}({attempt.failure or 'failed'})" for attempt in failures
        )
        return (
            f"all {len(self.adapters)} configured chat providers failed — {chain}. "
            f"{_last_detail(self.adapters)}"
        )

    def _refuse_unknown_kind(self, kind: str, adapter: ChatModel) -> None:
        """An adapter may only fail *technically*. See `TECHNICAL_FAILURES`.

        The check is here rather than only in `AnswerModelUnavailable.__init__` because a
        third-party or test adapter could raise a subclass that bypasses it — and a chain
        that degraded on an unclassified failure is a chain that could degrade on a poor
        answer, which is the one thing §5.3 forbids.
        """
        if kind not in TECHNICAL_FAILURES:
            raise AnswerModelUnavailable(
                f"{adapter.provider} reported {kind!r}, which is not one of the technical "
                f"failures a chain may degrade on ({TECHNICAL_FAILURES})"
            )


def stream_with_fallback(adapters: Sequence[ChatModel]) -> FallbackChatModel:
    """The chain, built from adapters. Named so a test can compose its own without settings."""
    return FallbackChatModel(adapters=tuple(adapters))


def _last_detail(adapters: Sequence[ChatModel]) -> str:
    """A stable sentence for the log: the last adapter's name, not its exception text.

    The provider's own message is on the exception the caller catches; repeating it in the
    chain's sentence would put a string that can name a key into a second place.
    """
    return f"the last provider tried was {adapters[-1].provider}"


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

    Since ticket 42 this builds **one** adapter and `build_chat_chain` composes several: the
    single-provider case is unchanged, which is what keeps an existing deployment's behaviour
    identical when it sets no `CHAT_PROVIDERS`.
    """
    if provider == "fake":
        return StreamedChatModel()
    if provider == "openai":
        return OpenAIChatModel(
            api_key or "",
            model=model,
            base_url=base_url,
            timeout=timeout,
            provider="openai",
            key_setting="OPENAI_API_KEY",
        )
    if provider == "deepseek":
        # The same adapter, because DeepSeek speaks the OpenAI dialect: `/chat/completions`,
        # `Authorization: Bearer`, `choices[0].delta.content`, `data: [DONE]`. One
        # implementation with two configurations is the honest model of that, and it is why
        # this branch names no new class.
        return OpenAIChatModel(
            api_key or "",
            model=model,
            base_url=base_url,
            timeout=timeout,
            provider="deepseek",
            key_setting="DEEPSEEK_API_KEY",
        )
    if provider == "anthropic":
        # A different dialect, so a different adapter: `/v1/messages`, `x-api-key`,
        # a top-level `system`, and `content_block_delta`. See `AnthropicChatModel`.
        return AnthropicChatModel(
            api_key or "", model=model, base_url=base_url, timeout=timeout
        )
    if provider == "ollama":
        # The OpenAI dialect with no key: a local runtime. See `KeylessChatModel`.
        if not api_key:
            return KeylessChatModel(
                provider="ollama", model=model, base_url=base_url, timeout=timeout
            )
        raise AnswerModelUnavailable(
            "the ollama adapter is keyless by design and OLLAMA_API_KEY was set; a local "
            "runtime that suddenly needs a key is a different deployment, not this one"
        )
    raise AnswerModelUnavailable(
        f"unknown chat provider {provider!r}; expected one of fake, openai, deepseek, "
        "anthropic, ollama. There is no 'none': an answer with no model would have to be "
        "written from the model's own knowledge, which is the fallback D20 forbids"
    )


__all__ = [
    "DEFAULT_API_VERSION",
    "DEFAULT_BASE_URL",
    "FAKE_MODEL",
    "NO_PASSAGES",
    "PROVIDERS",
    "REQUEST_TIMEOUT_SECONDS",
    "TECHNICAL_FAILURES",
    "AnthropicChatModel",
    "AnswerModelUnavailable",
    "ChatAttempt",
    "ChatModel",
    "FallbackChatModel",
    "KeylessChatModel",
    "OpenAIChatModel",
    "ProviderConfig",
    "StreamedChatModel",
    "UnavailableChatModel",
    "build_chat_chain",
    "build_chat_model",
    "failure_kind_for",
    "failure_kind_for_os_error",
    "parse_provider_chain",
    "stream_with_fallback",
]
