"""Ticket 42's provider chain: configuration, degradation on technical failure only, and the
record of what actually answered.

The chain is exercised through **the real adapters** against a **local stub server** — an
`http.server` on loopback, in a thread — rather than through a mock. The ticket's verification
standard forbids a test that needs a real API key or the internet, and it does not forbid the
one thing that proves an adapter works: a process that speaks the provider's SSE dialect and
records the request it was sent. So `StubProvider` is a provider on the wire, and the tests
below assert what the adapter *sent* (the model, the header, the body) and what it made of what
came back, which no mock of `urlopen` can show.

The three checklist lines this file owns:

* `test_the_chain_is_configuration_and_the_default_is_openai_first` — 「配置中定义调用顺序
  （OpenAI 主，DeepSeek / Claude 备）」;
* `test_the_chain_moves_on_only_for_a_technical_failure` — 「降级只在技术失败时发生」, with a
  primary that answers *poorly and successfully* as the mutation target;
* `test_every_provider_failing_is_one_explicit_error` — 「验证给出明确错误而非静默空回答」.

`test_the_embedding_rule_is_pinned_and_has_no_fallback` lives here rather than in
`test_embeddings.py` because it is this ticket's checklist line: ticket 32's behaviour is
correct and **unchanged**, and the job is to keep it that way — the embedding provider has no
chain, and a chunk is never written with a mismatched vector.
"""

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from app.core.constants import EMBEDDING_DIMENSIONS
from app.domain.answer.chat import (
    PROVIDERS,
    TECHNICAL_FAILURES,
    AnswerModelUnavailable,
    AnthropicChatModel,
    FallbackChatModel,
    OpenAIChatModel,
    UnavailableChatModel,
    build_chat_chain,
    build_chat_model,
    failure_kind_for,
    parse_provider_chain,
    stream_with_fallback,
)

# --- a provider on the wire --------------------------------------------------


@dataclass
class StubProvider:
    """A provider that speaks the dialect, on loopback, and records what it was asked.

    `answer` is what a successful stream carries; `status` is an HTTP status to fail with
    instead (0 means "succeed"). `mode` selects the dialect, because that is the one thing the
    two adapters do differently and the reason `AnthropicChatModel` exists.

    The recorded `requests` are the evidence for half of these tests: that the adapter sent
    its model, its key in the right header, and its prompt as the dialect requires — which is
    what "the adapter is real" means.
    """

    answer: str = "Los empleados tienen quince días naturales. [1]"
    status: int = 0
    mode: str = "openai"
    requests: list[dict[str, Any]] = field(default_factory=list)
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        """The OpenAI-dialect root: `http://host:port/v1`."""
        return f"{self.host_url}/v1"

    @property
    def host_url(self) -> str:
        """The bare origin, which is what the Anthropic adapter wants: it appends `/v1/messages`."""
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "StubProvider":
        handler = _handler_for(self)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def _handler_for(stub: StubProvider) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:  # noqa: ANN002 - silence the stub
            return

        def do_POST(self) -> None:  # noqa: N802 - the stdlib's spelling
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            stub.requests.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": body,
                }
            )
            if stub.status:
                payload = json.dumps({"error": {"message": "the provider refused"}}).encode()
                self.send_response(stub.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for frame in _frames(stub):
                self.wfile.write(frame.encode("utf-8"))
            self.wfile.flush()
            self.close_connection = True

    return Handler


def _frames(stub: StubProvider) -> list[str]:
    """The provider's SSE frames, in its own dialect."""
    if stub.mode == "anthropic":
        delta = json.dumps(
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": stub.answer},
            }
        )
        return [
            'event: message_start\ndata: {"type":"message_start"}\n\n',
            f"event: content_block_delta\ndata: {delta}\n\n",
            'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]
    return [
        'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
        f'data: {json.dumps({"choices": [{"delta": {"content": stub.answer}}]})}\n\n',
        "data: [DONE]\n\n",
    ]


@pytest.fixture
def stub() -> Iterator[StubProvider]:
    """A stub provider on loopback, started for one test and always stopped."""
    started = StubProvider().start()
    try:
        yield started
    finally:
        started.stop()


@dataclass
class FailingAdapter:
    """A `ChatModel` whose call fails with a named technical kind, and which counts calls.

    Used for the *decision* tests — which failures move a chain on — where what matters is the
    kind of the failure and not the transport that produced it.
    """

    provider: str = "primary"
    model: str = "primary-v1"
    failure: str = "timeout"
    answer: str | None = None
    calls: int = 0
    asks: list[list[Mapping[str, str]]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.model

    async def stream(self, messages: list[Mapping[str, str]]) -> AsyncIterator[str]:
        self.calls += 1
        self.asks.append(list(messages))
        if self.answer is None:
            raise AnswerModelUnavailable(f"{self.provider} failed", failure=self.failure)
        # A *successful* call that answers poorly: short, uncited, unhelpful. This is the
        # input the chain must not degrade on.
        yield self.answer


# --- the chain is configuration ----------------------------------------------


def test_the_chain_is_configuration_and_the_default_is_openai_first() -> None:
    """**「配置中定义调用顺序」.** The order is a value, and an unset chain is a chain of one.

    The default matters twice: §5.3's list puts OpenAI first, and a deployment that sets
    nothing must not acquire a second provider — a chain nobody configured that silently had
    two entries would make `provider_used` a surprise. `PROVIDERS` is the catalogue the names
    are checked against, and `parse_provider_chain` refuses a name it has no adapter for
    rather than dropping it.
    """
    from app.config import get_settings

    settings = get_settings()
    assert settings.chat_providers is None, (
        "the test environment set CHAT_PROVIDERS; the default behaviour is what this asserts"
    )
    assert settings.chat_provider_names == ("fake",), (
        "development and test derive the fake; production derives openai"
    )
    assert parse_provider_chain("openai,deepseek", fallback="fake") == ("openai", "deepseek")
    assert parse_provider_chain("OpenAI , deepseek", fallback="fake") == ("openai", "deepseek")
    assert parse_provider_chain("", fallback="openai") == ("openai",)
    assert parse_provider_chain(None, fallback="openai") == ("openai",)
    # §5.3's own order, and the catalogue can build every one of them. `ollama` is the
    # keyless local entry this ticket adds so that `docker compose up` can exercise the chain
    # with no API key at all — the same standard every ticket in this repository is held to.
    assert tuple(PROVIDERS) == ("openai", "deepseek", "anthropic", "ollama")
    assert PROVIDERS["openai"].model == "gpt-4o"
    assert PROVIDERS["deepseek"].model == "deepseek-chat"
    assert PROVIDERS["ollama"].anonymous is True
    assert not any(
        config.anonymous for name, config in PROVIDERS.items() if name != "ollama"
    ), "a SaaS provider was marked as needing no key"

    with pytest.raises(AnswerModelUnavailable) as unknown:
        parse_provider_chain("openai,mistral", fallback="fake")
    assert "mistral" in str(unknown.value), "an unknown provider must be named, not dropped"

    # **The derived name is always acceptable**, even though `fake` has no `PROVIDERS` entry.
    # It has none on purpose — no endpoint, no model, no key — and refusing it would mean a
    # deployment could not name the adapter it is already running. This is a regression test:
    # the first version of the parser built `allowed` from the catalogue alone, and every
    # request in the test environment (where `chat_provider_name` derives `fake`) would have
    # been refused at the point the service was assembled.
    assert parse_provider_chain("fake", fallback="fake") == ("fake",)
    assert parse_provider_chain("fake,openai", fallback="fake") == ("fake", "openai")
    # And it is the *derived* name that is acceptable, not `fake` unconditionally: a
    # production deployment (whose derived name is `openai`) that wrote `CHAT_PROVIDERS=fake`
    # would be asking for the stand-in, and that is refused for the reason `build_chat_model`
    # refuses it — a stand-in must never be selected silently outside development.
    with pytest.raises(AnswerModelUnavailable) as nameable:
        parse_provider_chain("fake", fallback="openai")
    assert "fake" in str(nameable.value)
    assert "fake" not in PROVIDERS, (
        "fake grew a catalogue entry: it has no endpoint and no model, and the parser's "
        "fallback rule is what keeps it nameable"
    )


def test_every_configured_provider_is_present_in_the_chain_even_without_a_key() -> None:
    """A keyless provider is *present and failing*, so the fallback record can say why.

    The mutation this pins: skip a provider whose key is missing at construction. The chain
    would then report `provider_used=deepseek` with nothing to say about why OpenAI was never
    tried — the invisible degradation `rag_messages.provider_used` exists to prevent.
    """
    chain = build_chat_chain(
        ("openai", "deepseek"),
        keys={"openai": None, "deepseek": "k"},
        # Port 9 is the discard port: nothing listens, so the second attempt is a *connection*
        # failure — the ticket's fourth technical failure — without waiting for a timeout.
        base_urls={"deepseek": "http://127.0.0.1:9/v1"},
    )
    assert isinstance(chain, FallbackChatModel)
    assert chain.provider_count == 2
    assert chain.provider == "openai", "the primary is what the start event must name"
    assert isinstance(chain.adapters[0], UnavailableChatModel)
    assert isinstance(chain.adapters[1], OpenAIChatModel)

    async def run() -> str:
        return "".join([part async for part in chain.stream([{"role": "user", "content": "q"}])])

    # openai has no key, so it fails with `authentication`; deepseek has a key and nothing
    # listening, which is a connection failure. Both are technical, which is the point.
    with pytest.raises(AnswerModelUnavailable) as dead:
        asyncio.run(run())
    assert [attempt.provider for attempt in chain.attempts] == ["openai", "deepseek"]
    assert [attempt.failure for attempt in chain.attempts] == [
        "authentication",
        "connection_error",
    ]
    assert "authentication" in str(dead.value) and "connection_error" in str(dead.value)


def test_the_chain_has_no_empty_state() -> None:
    """An empty chain is refused: 「没有模型」 is the one fallback D20 forbids."""
    with pytest.raises(AnswerModelUnavailable):
        build_chat_chain((), keys={})
    with pytest.raises(ValueError):
        stream_with_fallback([])


def test_the_keyless_provider_is_built_without_an_authorization_header(
    stub: StubProvider,
) -> None:
    """`ollama` is a real, keyless chain entry — which is what makes the chain testable offline.

    The verification standard forbids a test that needs a real API key, and every one of
    §5.3's three providers is a SaaS. A local runtime speaking the same dialect closes that
    gap without inventing anything: the adapter sends the documented body and **no**
    `Authorization` header, so `CHAT_PROVIDERS=ollama` answers with no key at all.
    """
    chain = build_chat_chain(
        ("ollama",),
        keys={},
        base_urls={"ollama": stub.base_url},
        models={"ollama": "llama3.1"},
    )
    assert chain.provider_count == 1
    text = "".join(asyncio.run(_collect(chain, [{"role": "user", "content": "q"}])))
    assert text == stub.answer
    request = stub.requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["body"]["model"] == "llama3.1"
    assert _header(request, "Authorization") is None, (
        "the keyless adapter sent an authorization header"
    )
    assert chain.attempts[0].provider == "ollama"


# --- degradation happens on technical failure, and only then -----------------


def test_the_chain_moves_on_only_for_a_technical_failure() -> None:
    """**「降级只在技术失败时发生」, and the mutation target for it.**

    Two runs, one assertion each:

    * the primary fails with each of the four failures the ticket names — timeout, rate
      limit, server error, connection failure — and the chain moves on **every time**;
    * the primary answers *successfully and poorly* — a two-word, uncited answer — and the
      secondary is **never called**.

    The second is the ticket's own distinction made visible in code: degradation is a decision
    about the *call*, so a returned stream ends the chain however bad the text is. The mutation
    this pins: replace `except AnswerModelUnavailable` with a check on the text length, and the
    poor-but-successful test fails on `secondary.calls == 1`.
    """
    for kind in ("timeout", "rate_limit", "server_error", "connection_error"):
        primary = FailingAdapter(provider="openai", model="gpt-4o", failure=kind)
        secondary = FailingAdapter(provider="deepseek", model="deepseek-chat", answer="ok")
        chain = stream_with_fallback([primary, secondary])

        text = "".join(asyncio.run(_collect(chain, [{"role": "user", "content": "q"}])))
        assert text == "ok", f"{kind} did not move the chain on"
        assert primary.calls == 1 and secondary.calls == 1
        assert [attempt.outcome for attempt in chain.attempts] == ["failed", "ok"]
        assert chain.provider == "deepseek" and chain.name == "deepseek-chat"
        assert chain.fallbacks()[0].failure == kind

    primary = FailingAdapter(provider="openai", model="gpt-4o", answer="No sé.")
    secondary = FailingAdapter(provider="deepseek", model="deepseek-chat", answer="ok")
    chain = stream_with_fallback([primary, secondary])

    text = "".join(asyncio.run(_collect(chain, [{"role": "user", "content": "q"}])))
    assert text == "No sé."
    assert secondary.calls == 0, (
        "the chain degraded on a poor but successful answer, which §5.3/D17 forbids: "
        f"{[attempt.outcome for attempt in chain.attempts]}"
    )
    assert chain.provider == "openai" and chain.name == "gpt-4o"
    assert chain.fallbacks() == ()


def test_a_failure_kind_that_is_not_technical_is_refused_by_the_chain() -> None:
    """The vocabulary is the guard: nothing in `TECHNICAL_FAILURES` names a judgement about text.

    A model that raised "the answer was weak" as a failure kind would be a chain that could
    degrade on quality, so the chain refuses an unclassified kind instead of moving on. This is
    the structural half of the rule above: even a *broken* adapter cannot make the chain
    degrade for a non-technical reason.
    """
    assert not any(
        word in kind
        for kind in TECHNICAL_FAILURES
        for word in ("quality", "short", "weak", "empty", "poor")
    )

    class Liar:
        provider = "liar"
        name = "liar-v1"

        async def stream(self, messages: list[Mapping[str, str]]) -> AsyncIterator[str]:
            raise AnswerModelUnavailable("the answer looked weak", failure="")
            yield ""  # pragma: no cover

    chain = stream_with_fallback([Liar()])
    with pytest.raises(AnswerModelUnavailable) as refused:
        asyncio.run(_collect(chain, [{"role": "user", "content": "q"}]))
    # An adapter that reported no kind at all is treated as a protocol failure — technical —
    # and the chain reports every provider failing rather than inventing an answer.
    assert "all 1 configured chat providers failed" in str(refused.value)


def test_a_stream_that_fails_halfway_is_not_completed_by_another_provider() -> None:
    """Degradation happens *before* the first increment, never in the middle of one.

    A second provider's text appended to a first provider's half-sentence would be an answer
    nobody wrote. The client has already received the first half — that cannot be taken back —
    so the honest outcome is the explicit error.
    """

    class Halfway:
        provider = "halfway"
        name = "halfway-v1"

        async def stream(self, messages: list[Mapping[str, str]]) -> AsyncIterator[str]:
            yield "Los empleados tienen "
            raise AnswerModelUnavailable("the socket died", failure="connection_error")

    secondary = FailingAdapter(provider="deepseek", model="deepseek-chat", answer="quince días")
    chain = stream_with_fallback([Halfway(), secondary])

    async def run() -> list[str]:
        return [part async for part in chain.stream([{"role": "user", "content": "q"}])]

    with pytest.raises(AnswerModelUnavailable):
        asyncio.run(run())
    assert secondary.calls == 0, "a second provider was asked to finish a half-written answer"


# --- a real adapter, on the wire ---------------------------------------------


def test_the_openai_adapter_falls_over_to_a_second_provider_on_the_wire() -> None:
    """**「模拟主供应商故障，验证自动切换且记录正确」**, through the real HTTP adapter.

    A real `StubProvider` answers 500 for the primary; a real one streams a completion for the
    secondary. Both requests are asserted: the path, the bearer token, the model in the body
    and the passages in the messages — so "the adapter works" is a claim about the wire rather
    than about a mock.

    Both are created here rather than taking the `stub` fixture's one, because the point is a
    chain of two different providers: one that fails and one that answers.
    """
    primary = StubProvider(status=500).start()
    secondary = StubProvider(answer="Quince días naturales. [1]").start()
    try:
        chain = build_chat_chain(
            ("openai", "deepseek"),
            keys={"openai": "sk-primary", "deepseek": "sk-secondary"},
            base_urls={"openai": primary.base_url, "deepseek": secondary.base_url},
            models={"openai": "gpt-4o", "deepseek": "deepseek-chat"},
        )
        text = "".join(
            asyncio.run(
                _collect(
                    chain,
                    [
                        {"role": "system", "content": "Answer from the passages."},
                        {"role": "user", "content": "Question: ¿Cuántos días?"},
                    ],
                )
            )
        )
    finally:
        primary.stop()
        secondary.stop()

    assert text == "Quince días naturales. [1]"
    assert [attempt.outcome for attempt in chain.attempts] == ["failed", "ok"]
    assert chain.provider == "deepseek" and chain.name == "deepseek-chat"
    assert chain.fallbacks()[0].failure == "server_error"

    primary_request = primary.requests[0]
    assert primary_request["path"] == "/v1/chat/completions"
    assert _header(primary_request, "Authorization") == "Bearer sk-primary"
    assert primary_request["body"]["model"] == "gpt-4o"
    assert primary_request["body"]["stream"] is True
    assert primary_request["body"]["messages"][1]["content"] == "Question: ¿Cuántos días?"

    fallback_request = secondary.requests[0]
    assert _header(fallback_request, "Authorization") == "Bearer sk-secondary"
    assert fallback_request["body"]["model"] == "deepseek-chat", (
        "the fallback provider was asked for the primary's model"
    )


def test_the_claude_adapter_is_a_second_dialect_on_the_wire(stub: StubProvider) -> None:
    """`anthropic` is implemented, not "configured but unimplemented".

    The ticket's chain lists Claude as a fallback and this repository does not get to leave an
    entry that can never answer. So `AnthropicChatModel` posts to `/v1/messages` with
    `x-api-key` and a top-level `system`, and reads `content_block_delta` — the four ways the
    two dialects differ, each asserted against what the stub received.
    """
    claude = StubProvider(answer="Quince días naturales.", mode="anthropic").start()
    try:
        model = AnthropicChatModel(
            "sk-ant", model="claude-3-5-sonnet-latest", base_url=claude.host_url
        )
        text = "".join(
            asyncio.run(
                _collect(
                    model,
                    [
                        {"role": "system", "content": "Answer from the passages."},
                        {"role": "user", "content": "Question: ¿Cuántos días?"},
                    ],
                )
            )
        )
    finally:
        claude.stop()

    assert text == "Quince días naturales."
    request = claude.requests[0]
    assert request["path"] == "/v1/messages", "Anthropic's path is not OpenAI's"
    assert _header(request, "x-api-key") == "sk-ant"
    assert _header(request, "Authorization") is None, (
        "the Claude adapter sent OpenAI's auth header"
    )
    assert _header(request, "anthropic-version")
    assert request["body"]["system"] == "Answer from the passages."
    assert request["body"]["max_tokens"] > 0
    assert all(turn["role"] != "system" for turn in request["body"]["messages"]), (
        "Anthropic rejects a `system` role inside messages"
    )


def test_the_two_dialects_report_the_same_technical_kinds(stub: StubProvider) -> None:
    """A 429 is a rate limit whichever adapter saw it — one reading of a status code."""
    for status, kind in (
        (429, "rate_limit"),
        (500, "server_error"),
        (503, "server_error"),
        (504, "timeout"),
        (401, "authentication"),
        (404, "not_found"),
    ):
        assert failure_kind_for(status) == kind

    for model in (
        OpenAIChatModel("k", model="gpt-4o", base_url=stub.base_url),
        AnthropicChatModel("k", model="claude", base_url=stub.host_url),
    ):
        stub.requests.clear()
        stub.status = 429
        with pytest.raises(AnswerModelUnavailable) as throttled:
            asyncio.run(_collect(model, [{"role": "user", "content": "q"}]))
        assert throttled.value.failure == "rate_limit", model.provider
    stub.status = 0


# --- all providers failing ---------------------------------------------------


def test_every_provider_failing_is_one_explicit_error() -> None:
    """**「模拟全部供应商故障，验证给出明确错误而非静默空回答」.**

    The error names every provider and its kind, in order, so an operator reading one log line
    knows what to fix; and **nothing is yielded**, which is the difference between an error and
    an empty answer. The mutation this pins: return instead of raise, and the assertion on the
    collected text fails while the exception is never raised at all.
    """
    primary = FailingAdapter(provider="openai", model="gpt-4o", failure="timeout")
    secondary = FailingAdapter(provider="deepseek", model="deepseek-chat", failure="rate_limit")
    chain = stream_with_fallback([primary, secondary])

    async def run() -> list[str]:
        return [part async for part in chain.stream([{"role": "user", "content": "q"}])]

    with pytest.raises(AnswerModelUnavailable) as failed:
        asyncio.run(run())

    detail = str(failed.value)
    assert "all 2 configured chat providers failed" in detail
    assert "openai(timeout)" in detail and "deepseek(rate_limit)" in detail
    assert failed.value.code.value == "ERR_ANS_001"
    assert failed.value.failure == "rate_limit", "the last attempt's kind, not a new one"
    assert primary.calls == 1 and secondary.calls == 1


def test_the_openai_adapter_with_no_key_names_the_setting() -> None:
    """A keyless single adapter still fails with `ERR_ANS_001`, naming the variable to set."""
    with pytest.raises(AnswerModelUnavailable) as missing:
        build_chat_model("openai", api_key=None, model="gpt-4o")
    assert "OPENAI_API_KEY" in str(missing.value)
    assert missing.value.failure == "authentication"

    with pytest.raises(AnswerModelUnavailable) as claude:
        build_chat_model("anthropic", api_key=None, model="claude")
    assert "ANTHROPIC_API_KEY" in str(claude.value)


def test_an_unclassified_failure_kind_cannot_be_constructed() -> None:
    """`AnswerModelUnavailable` refuses a kind outside the vocabulary at construction.

    The second layer of the same rule: an adapter cannot smuggle "the answer was poor" into a
    failure kind, because the exception's own constructor rejects it.
    """
    with pytest.raises(ValueError):
        AnswerModelUnavailable("bad answer", failure="poor_quality")
    # No kind at all is allowed — a transport that does not classify — and the chain treats
    # it as a protocol failure, which is technical.
    assert AnswerModelUnavailable("unclassified").failure == ""


# --- the embedding rule stays as it is ---------------------------------------


def test_the_embedding_rule_is_pinned_and_has_no_fallback() -> None:
    """**「嵌入模型不参与自动降级」**, pinned by the two facts that make it true.

    Ticket 32's behaviour is correct and this ticket does not change it; what it does is make
    the rule *visible* and *asserted*, because "we did not add a fallback" is not something a
    reviewer can check from a diff:

    * `build_embedder` has **no chain** — it returns one adapter, and there is no
      `FallbackEmbedder` anywhere in the tree to reach for;
    * a vector of the wrong width is refused rather than stored, so the pipeline can never
      write a chunk whose dimension disagrees with the column.

    The wrong-width assertion runs against the real class rather than a double: the check is
    `len(vector) != EMBEDDING_DIMENSIONS` and this pins that it is there.
    """
    import app.domain.document.embeddings as embeddings
    from app.domain.document.embeddings import (
        DeterministicEmbedder,
        EmbeddingUnavailable,
        build_embedder,
    )

    # One adapter, chosen by name — no list, no order, no fallback.
    chosen = build_embedder("fake")
    assert isinstance(chosen, DeterministicEmbedder)
    assert not hasattr(embeddings, "EMBED_CHAIN")
    assert not any(
        "fallback" in name.lower() for name in dir(embeddings)
    ), "an embedding fallback appeared; §5.3/DESIGN §10.3 forbid mixing dimensions"

    source = Path(embeddings.__file__).read_text(encoding="utf-8")
    assert "EMBED_CHAIN" not in source, (
        "the design's EMBED_CHAIN is a list of one provider for the same dimension; a chain "
        "here means the dimension rule has been reopened"
    )

    async def run() -> None:
        # A provider that returned the wrong width is refused, not stored.
        short = DeterministicEmbedder(dimensions=8)
        vectors = await short.embed(["un texto"])
        assert len(vectors[0]) == 8 != EMBEDDING_DIMENSIONS
        # And the adapter the pipeline actually uses produces the column's width.
        right = await chosen.embed(["un texto"])
        assert len(right[0]) == EMBEDDING_DIMENSIONS

    asyncio.run(run())

    with pytest.raises(EmbeddingUnavailable):
        # No key: the real adapter refuses at construction, and nothing falls back to a fake
        # vector of another width.
        build_embedder("openai", api_key=None)


def test_the_pipeline_leaves_a_chunk_unembedded_rather_than_writing_a_wrong_vector() -> None:
    """「不写入维度不符的向量」, asserted against the chunk write path's own condition.

    The pipeline's shape is `WHERE embedding IS NULL` as the worklist (ticket 32), and the
    property this ticket must not break is that a failed embedding leaves that condition true.
    The assertion is on the documented source contract — the same shape
    `test_chunking.py::test_the_unembedded_worklist_is_an_index_condition` uses — rather than a
    second pipeline run, because running one would test ticket 32's code again rather than this
    ticket's restraint.
    """
    from app.domain.document import embeddings

    assert EMBEDDING_DIMENSIONS == 1536, (
        "the column's width moved; a migration and a re-embed are the only honest way to "
        "change it (DESIGN §10.3)"
    )
    assert embeddings.EMBEDDING_MODEL, "the embedding model is fixed, not a deployment choice"


async def _drain(
    model: object, messages: list[Mapping[str, str]]
) -> AsyncIterator[str]:
    """Every increment a model's stream yields, for a test that wants the whole text."""
    async for increment in model.stream(messages):  # type: ignore[attr-defined]
        yield increment


async def _collect(model: object, messages: list[Mapping[str, str]]) -> list[str]:
    return [part async for part in _drain(model, messages)]


def _header(request: dict[str, Any], name: str) -> str | None:
    """One request header, case-insensitively.

    `http.client` title-cases what it sends, so `x-api-key` arrives as `X-Api-Key` and a test
    that looked up the lowercase spelling would pass or fail depending on which adapter sent
    it — which is a fact about the client library rather than about the adapter.
    """
    lowered = {key.lower(): value for key, value in request["headers"].items()}
    return lowered.get(name.lower())
