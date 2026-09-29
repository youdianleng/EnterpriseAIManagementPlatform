"""Ticket 38: the graph, its five routes, its code-layer refusal and its persisted pause.

Seven checklist lines, and the test that pins each:

* 意图分类节点覆盖五种结果 —
  `test_the_classifier_decides_each_of_the_five_outcomes`
* 制度问答复用已有检索、流式与引用 —
  `test_a_policy_answer_is_the_existing_pipelines_own_stream_event_for_event`
* 硬禁止在代码层被拒绝，不转发给模型 —
  `test_each_prohibited_ask_is_refused_without_calling_the_model_or_the_answer_path`
* 中间状态持久化在独立 schema、不用 Redis —
  `test_the_pause_is_written_to_the_langgraph_schema_and_not_to_redis`
* 中断后重启仍能继续 —
  `test_an_interrupted_run_resumes_on_a_graph_rebuilt_against_the_same_database`
* 拓扑以代码表达、单测可直接调用 —
  `test_the_routing_table_covers_the_five_intents_and_names_real_nodes`
* 节点记录输入输出且不含正文 —
  `test_records_carry_names_counts_and_timings_and_never_content`

**Real infrastructure, and only the two sanctioned seams.** PostgreSQL is real (the corpus is
uploaded through the HTTP endpoint and parsed by ticket 32's pipeline, the retrieval is
ticket 33's SQL), Redis is real, and the checkpointer is the real
`langgraph-checkpoint-postgres` writing to the real `langgraph` schema. Two doubles appear,
both of them the repository's own adapter pattern rather than mocks:

* `StreamedChatModel` — ticket 34's development adapter, deterministic and offline, behind
  the `ChatModel` seam the design records as real (§4's table);
* `RecordingChatModel`, defined here, whose only job is the claim "the model was **not**
  called": that is a statement about calls, and nothing but a recorder can make it. Ticket
  34's `test_answer.py` defines the same double for the same reason.

**What the restart test actually does**, because it is the one this ticket exists for: it
pauses a run with one `AsyncPostgresSaver`, closes it — connection and all — opens a *new*
saver on a new connection, compiles a *new* graph object, hands it a *new* context, and
resumes the same thread id with `Command(resume=…)`. Every fact the second run uses comes
from the database. `test_resuming_does_not_re_run_the_nodes_before_the_pause` asserts the
other half: the classification happened once, not twice.
"""

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from langgraph.types import Command

from app.ai.agents import (
    CONFIRMATION_NODE,
    DRAFT_BRANCH,
    NODES,
    REFUSALS,
    ROUTES,
    SMALL_TALK_REPLY,
    TERMINAL_BRANCHES,
    AgentContext,
    Intent,
    NotAForbiddenRule,
    UnroutableIntent,
    build_graph,
    classify,
    nodes_of,
    open_checkpointer,
    refusal_for,
    thread_config,
)
from app.ai.agents.checkpoint import CHECKPOINT_SCHEMA, checkpoint_dsn
from app.ai.agents.graph import branch_for, branch_of
from app.ai.agents.intents import FORBIDDEN_RULES
from app.ai.agents.records import ALLOWED_FIELDS, node_names, records_of
from app.ai.agents.state import AgentState
from app.ai.tools import ToolKind, ToolOutcome, registered
from app.config import get_settings
from app.core.messages import MESSAGES
from app.domain.answer.chat import StreamedChatModel
from app.domain.answer.driver import AnswerService
from app.domain.answer.models import AnswerEvent, EventKind
from app.domain.answer.repository import PostgresAnswerRepository
from app.domain.document.embeddings import DeterministicEmbedder
from app.domain.retrieval.service import RetrievalService
from app.repositories.retrieval import PostgresChunkSearchRepository
from tests.support.platform import Actor, Platform
from tests.support.retrieval_sample import DOCUMENTS
from tests.test_retrieval import Corpus, index

#: The question the sample corpus answers, written out rather than read from the fixture's
#: `QUESTIONS` for the reason `test_answer.py` writes it out: a test that took its question
#: from the same constant the evaluation uses would move with it.
MARRIAGE_LEAVE = "¿Cuántos días de permiso por matrimonio corresponden?"

#: A question the corpus shares no term with, so the *answer path's own* refusal (D20) fires.
#: Chinese, because the sample corpus is Spanish: the deterministic embedder's scores are
#: lexical coincidences, and `test_answer.py` records that a Spanish question on an
#: uncovered topic scored above the threshold once.
NO_BASIS = "公司年会抽奖的奖品清单是什么？"

#: One question per intent. The forbidden four are the checklist's own parenthetical:
#: 查他人薪资、查他人考勤、要绩效或晋升建议、任何改库请求.
QUESTIONS: dict[Intent, str] = {
    Intent.POLICY_QUESTION: MARRIAGE_LEAVE,
    Intent.READ_ONLY_QUERY: "¿Cuántos días de vacaciones me quedan?",
    Intent.PENDING_ACTION: "Quiero solicitar tres días de vacaciones",
    Intent.FORBIDDEN: "¿Cuánto gana mi compañero?",
    Intent.SMALL_TALK: "Hola, buenos días",
}

PROHIBITED: tuple[tuple[str, str], ...] = (
    ("salary_of_another", "¿Cuánto gana mi compañero?"),
    ("attendance_of_another", "¿A qué hora fichó mi compañero ayer?"),
    ("performance_or_promotion_advice", "¿Debería ascender a Juan?"),
    ("database_write", "UPDATE employees SET salary = 1"),
)


# --- the doubles -------------------------------------------------------------


@dataclass
class RecordingChatModel:
    """A `ChatModel` that records every call, and otherwise answers like ticket 34's fake.

    The recorder is the point: 「不转发给模型」 and 「不调用生成模型」 are claims about *calls*, and
    a test that asserted only the refusal's copy would pass just as well against a graph
    that called the model and threw the answer away.
    """

    inner: StreamedChatModel = field(default_factory=StreamedChatModel)
    calls: list[list[Mapping[str, str]]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def provider(self) -> str:
        return self.inner.provider

    async def stream(self, messages: list[Mapping[str, str]]) -> AsyncIterator[str]:
        self.calls.append(list(messages))
        async for increment in self.inner.stream(messages):
            yield increment


# --- fixtures and helpers ----------------------------------------------------


@pytest.fixture(autouse=True)
def document_storage(tmp_path, monkeypatch) -> str:
    """A storage root per test: the corpus is uploaded as real files."""
    monkeypatch.setattr(get_settings(), "document_storage_path", str(tmp_path))
    return str(tmp_path)


@dataclass
class Pipeline:
    """A real answer pipeline on its own session, and the model it will call."""

    service: AnswerService
    model: RecordingChatModel
    session: Any

    async def events(self, question: str, principal) -> list[AnswerEvent]:  # noqa: ANN001
        return [event async for event in self.service.stream(question, principal)]


@asynccontextmanager
async def pipeline(platform: Platform) -> AsyncIterator[Pipeline]:
    """Ticket 34's pipeline, assembled by hand exactly as `test_answer.py` assembles it.

    **The session is closed on the way out**, and that is not tidiness: a retrieval opens an
    implicit transaction, so a session left open sits `idle in transaction` and the next
    test's `TRUNCATE … CASCADE` waits for it — the hang `test_retrieval.py` documents.

    A hand-built service rather than the HTTP endpoint, because this module tests the
    *graph*: the endpoint is ticket 34's subject, and a graph that needed HTTP to be
    exercised would fail the checklist line that says it must not.
    """
    settings = get_settings()
    session = platform.factory()
    model = RecordingChatModel()
    retrieval = RetrievalService(
        PostgresChunkSearchRepository(session),
        embedder=DeterministicEmbedder(),
        min_score=settings.retrieval_min_score,
    )
    built = Pipeline(
        service=AnswerService(
            PostgresAnswerRepository(session), retrieval, model, session=session
        ),
        model=model,
        session=session,
    )
    try:
        yield built
    finally:
        await session.close()


async def principal_of(platform: Platform, actor: Actor):  # noqa: ANN201
    """The principal the endpoints build, resolved from the real snapshot.

    Not constructed by hand: a hand-built `Principal` would let this module's routing agree
    with a permission snapshot production never produces.
    """
    from app.domain.access.snapshot import resolve_principal

    async with platform.factory() as session:
        principal = await resolve_principal(session, UUID(actor.user_id))
        assert principal is not None
        return principal


async def company_week(platform: Platform, *, code: str = "graphweek") -> None:
    """A company week, so a *date range* is a number of working days.

    Ticket 40's draft branch validates with the leave module's own rules, and the first of
    those is 「is this a working day」 — asked of `ScheduleService`, which answers "no" for
    every day of an employee no schedule reaches. A test that drafts leave therefore needs a
    calendar, and the default schedule is the company-wide one that every employee without
    their own inherits. `test_leave.py::staff` builds the same week for the same reason.
    """
    admin = await platform.admin()
    created = await admin.post(
        "/api/v1/schedules",
        json={
            "code": code,
            "name_es": "Semana completa",
            "name_en": "Full week",
            "is_default": True,
            "days": [
                {
                    "weekday": weekday,
                    "expected_minutes": 480,
                    "start_time": "08:00",
                    "end_time": "16:00",
                }
                for weekday in range(5)
            ],
        },
    )
    assert created.status_code == 201, created.text


@dataclass
class Agent:
    """A compiled graph, one context and one thread — what a test drives.

    The thread id is per-`Agent` unless the test names one, because two runs that shared a
    thread would share a checkpoint and the second would resume the first.
    """

    graph: Any
    context: AgentContext
    thread: str

    @property
    def config(self) -> dict:
        return thread_config(self.thread)

    async def run(
        self,
        question: str | None = None,
        *,
        resume: Any = None,
        tool: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> dict:
        """One run: a question, or a resumption of the thread's stored pause.

        `tool` and `arguments` name a tool directly — the seam ticket 42 replaces with a
        model's function call, and what a test uses to exercise the draft branch without
        writing a sentence a lexical layer would have to happen to read. Ticket 39's
        read-only tests do the same for the read half.
        """
        if resume is not None:
            payload: Any = Command(resume=resume)
        else:
            payload = {"question": question}
            if tool is not None:
                payload["tool"] = tool
            if arguments is not None:
                payload["tool_arguments"] = dict(arguments)
        return await self.graph.ainvoke(payload, self.config, context=self.context)

    async def stream(self, question: str) -> tuple[dict, list[AnswerEvent]]:
        """One run, keeping what the nodes wrote to the custom stream.

        `stream_mode="custom"` is the pipe `answer_policy` forwards ticket 34's events
        through, so this is the client's view of a policy answer rather than a copy of it.
        """
        events: list[AnswerEvent] = []
        state: dict = {}
        async for mode, chunk in self.graph.astream(
            {"question": question},
            self.config,
            context=self.context,
            stream_mode=["custom", "values"],
        ):
            if mode == "custom":
                events.append(chunk)
            else:
                state = chunk
        return state, events


@asynccontextmanager
async def agent(
    answers: Pipeline,
    principal,  # noqa: ANN001
    *,
    thread: str | None = None,
    conversation_id: UUID | None = None,
) -> AsyncIterator[Agent]:
    """A graph with the real Postgres checkpointer, on this test's database.

    The context carries the pipeline's session (ticket 40): the draft branch validates
    against the same database a write would, and reads that session — the wiring
    `AgentContext.session` documents, and the reason a branch that touches the database
    raises rather than answering when it is missing.
    """
    context = AgentContext(
        principal=principal,
        answers=answers.service,
        conversation_id=conversation_id,
        session=answers.session,
    )
    async with open_checkpointer(get_settings(), test=True) as saver:
        yield Agent(
            graph=build_graph(checkpointer=saver),
            context=context,
            thread=thread or uuid4().hex,
        )


async def corpus_for(platform: Platform, cast) -> Corpus:  # noqa: ANN001
    """The sample corpus through the real upload and parse, for the policy tests."""
    return await index(platform, cast, *DOCUMENTS)


def _normalised(event: AnswerEvent) -> tuple[str, str, str]:
    """A `AnswerEvent` without the three fields two runs cannot share.

    `message_id` and `conversation_id` are minted per run and `latency_ms` is a measurement;
    everything else — the event name, the increment, the citations, the token counts, the
    refusal — must be identical if the graph is relaying the pipeline rather than producing
    its own version of it.
    """
    dropped = {"message_id", "conversation_id", "latency_ms"}
    data = {key: value for key, value in event.data.items() if key not in dropped}
    return (str(event.kind), event.text, json.dumps(data, sort_keys=True, default=str))


# --- 6: the topology, in code, without HTTP ----------------------------------


#: The routing table **as the report and DESIGN §6.1 state it**, written out rather than
#: read from `ROUTES`. Asserting `branch_of(...) == ROUTES[intent]` would be a tautology: it
#: compares the table with itself and passes while the table says anything at all. This is
#: the independent copy, and it is what a misrouted intent fails against.
EXPECTED_BRANCHES: dict[Intent, str] = {
    Intent.POLICY_QUESTION: "answer_policy",
    Intent.READ_ONLY_QUERY: "read_only_tools",
    Intent.PENDING_ACTION: DRAFT_BRANCH,
    Intent.FORBIDDEN: "refuse",
    Intent.SMALL_TALK: "small_talk",
}


def test_the_routing_table_covers_the_five_intents_and_names_real_nodes() -> None:
    """Checklist: 「图的拓扑以代码表达且可被单测直接调用（不需要走 HTTP）」.

    No database and no HTTP: `build_graph()` with no checkpointer compiles the topology, and
    everything asserted is a value a reader can also read in `graph.py`.
    """
    assert set(ROUTES) == set(Intent), "the routing table must cover exactly the five intents"
    assert set(ROUTES.values()) == set(TERMINAL_BRANCHES) | {DRAFT_BRANCH}
    for intent, branch in ROUTES.items():
        assert branch in NODES, f"{intent} routes to {branch}, which is not a node"
        assert branch_for(intent) == branch
    assert dict(ROUTES) == EXPECTED_BRANCHES, "an intent is routed to the wrong branch"

    compiled = build_graph()
    assert nodes_of(compiled) == set(NODES), "the compiled graph's nodes are not NODES"


def test_an_unknown_intent_is_refused_rather_than_routed_somewhere() -> None:
    """The router's failure mode: a decision with no branch must be loud.

    `branch_of` is the function LangGraph calls, so a corrupted or unknown intent reaches
    the graph through it. Defaulting to a branch would answer a question nobody asked.
    """
    with pytest.raises(UnroutableIntent):
        branch_for("policy")  # type: ignore[arg-type] - deliberately not an Intent
    with pytest.raises(UnroutableIntent):
        branch_of({"question": "irrelevant", "intent": "not_an_intent"})


def test_the_classifier_decides_each_of_the_five_outcomes() -> None:
    """Checklist: 分类结果至少覆盖 制度问答、只读数据查询、待办操作、硬禁止请求、闲聊.

    Each representative question is classified *and* routed, and the expected branch comes
    from `EXPECTED_BRANCHES` rather than from `ROUTES` — see that constant for why comparing
    the table with itself would prove nothing.
    """
    for intent, question in QUESTIONS.items():
        classification = classify(question)
        assert classification.intent is intent, f"{question!r} → {classification}"
        assert branch_of({"question": question, "intent": str(intent)}) == EXPECTED_BRANCHES[intent]


# --- 3: the four prohibitions, refused in code -------------------------------


async def test_each_prohibited_ask_is_refused_without_calling_the_model_or_the_answer_path(
    platform: Platform, cast
) -> None:
    """Checklist: 硬禁止请求…在**代码层**被拒绝…不转发给模型去"委婉处理".

    Four asks, D23's categories, and for each one three separate facts: the branch refused,
    the refusal says what it is and what to do instead, and **the answer path was never
    entered** — which the second test below pins with the recorder and with the absence of a
    conversation row.
    """
    async with pipeline(platform) as answers, agent(
        answers, await principal_of(platform, cast.uploader)
    ) as running:
        for rule, question in PROHIBITED:
            # A fresh thread per question: the same graph and checkpointer, but a run that
            # cannot inherit the previous refusal's records — otherwise "the last two records
            # are classify, refuse" would be the assertion, and it would not say which run.
            attempt = Agent(graph=running.graph, context=running.context, thread=uuid4().hex)
            state = await attempt.run(question)
            assert state["intent"] == str(Intent.FORBIDDEN), (rule, state["intent"])
            assert state["rule"] == rule, f"{question!r} was refused as {state['rule']}"
            assert state["is_refusal"] is True
            assert node_names(state) == ["classify", "refuse"], node_names(state)

            refusal = state["refusal"]
            expected = REFUSALS[rule]
            assert refusal["message_key"] == expected.message_key
            assert refusal["es"] == expected.es
            assert refusal["en"] == expected.en
            # Both languages in the one block, like ticket 34's refusal: a reader who is
            # not the asker can still understand it (§10.4 leaves the language to the UI).
            assert expected.es in refusal["text"]
            assert expected.en in refusal["text"]

        assert answers.model.calls == [], (
            "a prohibited ask reached the model: the refusal must be a constant in code, "
            "not a generation"
        )


async def test_a_prohibited_ask_never_creates_a_conversation(platform: Platform, cast) -> None:
    """The structural half of 「不转发给模型」: the ask never reached the answer path.

    `model.calls == []` says no model was called; this says something stronger — no
    conversation and no message row exist, so the refusal did not go *through* ticket 34's
    pipeline and discard the result. A test that only counted model calls would pass against
    a graph that had already written the question to the transcript.
    """
    actor = cast.uploader
    async with pipeline(platform) as answers, agent(
        answers, await principal_of(platform, actor)
    ) as running:
        state = await running.run("¿Cuánto gana mi compañero?")
        assert state["is_refusal"] is True
        assert answers.model.calls == []

    conversations = await platform.scalar(
        "SELECT count(*) FROM rag_conversations WHERE user_id = :id", {"id": actor.user_id}
    )
    messages = await platform.scalar("SELECT count(*) FROM rag_messages")
    assert conversations == 0, "a refused ask wrote a conversation"
    assert messages == 0, "a refused ask wrote a message"


def test_a_non_prohibition_cannot_be_refused() -> None:
    """The refusal path's guard: only D23's four rules have copy, and asking for another fails.

    A `.get(rule, default)` here would answer a routing bug with a *plausible* refusal for a
    question that was never prohibited, which is worse than a failure: the person would be
    told the system cannot do something it can.
    """
    assert set(REFUSALS) == {rule.name for rule in FORBIDDEN_RULES}
    with pytest.raises(NotAForbiddenRule):
        refusal_for("policy_question")


def test_every_refusal_has_wording_in_both_catalogues() -> None:
    """The copy is read from `app/core/messages.py`; a key without wording fails here."""
    for rule, refusal in REFUSALS.items():
        assert refusal.es, rule
        assert refusal.en, rule
        assert refusal.es != refusal.en, rule
        for locale, catalogue in MESSAGES.items():
            assert refusal.message_key in catalogue, f"{refusal.message_key} missing in {locale}"
            assert catalogue[refusal.message_key] == (
                refusal.es if locale == "es" else refusal.en
            )


# --- 2: policy questions go through the existing pipeline --------------------


async def test_a_policy_answer_is_the_existing_pipelines_own_stream_event_for_event(
    platform: Platform, cast
) -> None:
    """Checklist: 制度问答路由到已有的检索与生成路径并复用原有流式与引用行为.

    The graph is asked the question, and ticket 34's service is asked the same question over
    the same corpus by the same principal. The two event streams are then compared event by
    event, with only the ids and the timing removed — so this asserts *reuse* rather than
    resemblance. A graph with its own prompt, its own citation list or its own buffering
    would differ in the deltas, in the citation payloads, or in the number of increments.
    """
    corpus = await corpus_for(platform, cast)
    principal = await principal_of(platform, corpus.admin)

    async with pipeline(platform) as direct:
        direct_events = await direct.events(MARRIAGE_LEAVE, principal)
    async with pipeline(platform) as answers, agent(answers, principal) as running:
        state, forwarded = await running.stream(MARRIAGE_LEAVE)
        graph_model_calls = len(answers.model.calls)

    assert graph_model_calls == 1, "the graph must call the model once, through the service"
    assert forwarded, "the graph forwarded no events to its caller"
    assert [_normalised(event) for event in forwarded] == [
        _normalised(event) for event in direct_events
    ], "the graph's stream is not the pipeline's own stream"

    # And the summary the node kept, which is what the checkpoint carries instead of text.
    assert state["answer"]["outcome"] == "answered"
    assert state["answer"]["delta_count"] == sum(
        1 for event in forwarded if event.kind is EventKind.DELTA
    )
    assert state["answer"]["delta_count"] > 1, "the answer arrived in a single increment"
    assert state["answer"]["citation_count"] > 0
    assert state["answer"]["token_out"] > 0
    assert state["answer"]["model"] == StreamedChatModel().name
    assert state["conversation_id"] == state["answer"]["conversation_id"]


async def test_a_policy_answer_carries_the_pipelines_citations(platform: Platform, cast) -> None:
    """The citation half of the same line: what the graph relays names a real document."""
    corpus = await corpus_for(platform, cast)
    async with pipeline(platform) as answers, agent(
        answers, await principal_of(platform, corpus.admin)
    ) as running:
        _state, forwarded = await running.stream(MARRIAGE_LEAVE)

    citations = [
        event.data["citations"] for event in forwarded if event.kind is EventKind.CITATIONS
    ]
    assert citations, "a grounded answer must announce its citations before any text"
    first = citations[0][0]
    assert first["document_id"] in corpus.ids.values()
    assert first["filename"]
    assert first["quote"], "a citation without the passage it quotes is not a citation"


async def test_the_pipelines_own_refusal_comes_through_the_graph(platform: Platform, cast) -> None:
    """The other half of reuse: D20's refusal is ticket 34's, not a second one written here.

    No basis in the corpus means the pipeline refuses **without calling the model**, and the
    graph relays that refusal unchanged — the same `message_key`, `model_called: false`, and
    the same empty citation list.
    """
    await corpus_for(platform, cast)

    async with pipeline(platform) as answers, agent(
        answers, await principal_of(platform, cast.uploader)
    ) as running:
        state, forwarded = await running.stream(NO_BASIS)
        calls = list(answers.model.calls)

    assert state["answer"]["outcome"] == "refused"
    assert state["is_refusal"] is True
    assert calls == [], "the pipeline called the model on a question with no basis"
    refusal = [event for event in forwarded if event.kind is EventKind.REFUSAL]
    assert refusal, "the graph did not relay the refusal"
    assert refusal[0].data["model_called"] is False
    assert refusal[0].data["message_key"] == "errors.knowledge_base_no_basis"
    assert [event for event in forwarded if event.kind is EventKind.DELTA] == []


# --- 7: the records ----------------------------------------------------------


async def test_records_carry_names_counts_and_timings_and_never_content(
    platform: Platform, cast
) -> None:
    """Checklist: 每个节点的输入输出被记录，但不含对话正文与检索内容.

    Three assertions, and the third is the one the ticket names:

    1. every record's fields are exactly `records.ALLOWED_FIELDS` — the subset of DESIGN
       §10.1's `ALLOWED_TRACE_FIELDS` that a node can produce;
    2. `input_keys`/`output_keys` are state field *names*, and the counts are real (the
       question's length, the number of deltas, the number of citations);
    3. neither the question nor the passage the answer quoted appears anywhere in the
       serialised records. The passage comes from the `citations` event of the same run, so
       the test is asserting about the text the retrieval actually produced rather than
       about a constant.
    """
    corpus = await corpus_for(platform, cast)
    async with pipeline(platform) as answers, agent(
        answers, await principal_of(platform, corpus.admin)
    ) as running:
        state, forwarded = await running.stream(MARRIAGE_LEAVE)
        records = records_of(state)

    assert node_names(state) == ["classify", "answer_policy"]
    for record in records:
        assert set(record) == set(ALLOWED_FIELDS), record
        assert record["node_name"] in NODES
        assert isinstance(record["latency_ms"], int) and record["latency_ms"] >= 0
        assert set(record["input_keys"]) <= set(AgentState.__annotations__)
        assert set(record["output_keys"]) <= set(AgentState.__annotations__)

    classify_record, answer_record = records
    assert classify_record["decision"] == str(Intent.POLICY_QUESTION)
    assert classify_record["counts"] == {"question_chars": len(MARRIAGE_LEAVE)}
    assert answer_record["decision"] == "answered"
    assert answer_record["counts"]["deltas"] > 1
    assert answer_record["counts"]["citations"] > 0

    serialised = json.dumps(records, ensure_ascii=False)
    assert MARRIAGE_LEAVE not in serialised, "the question is in the records"
    passages = [
        citation["quote"]
        for event in forwarded
        if event.kind is EventKind.CITATIONS
        for citation in event.data["citations"]
    ]
    assert passages, "the run retrieved nothing, so this assertion would be vacuous"
    for passage in passages:
        assert passage not in serialised, "retrieved content is in the records"
        # The passage's own first sentence as well as the whole of it, for the case where a
        # record had kept a truncated copy. Only long enough excerpts are asserted on: a
        # three-character fragment would be found inside a latency figure.
        excerpt = passage.split(".")[0]
        if len(excerpt) >= 20:
            assert excerpt[:60] not in serialised
    for document in DOCUMENTS:
        marker = document.body.strip().splitlines()[0][:40]
        if len(marker) >= 12:
            assert marker not in serialised, f"the text of {document.title} is in the records"


async def test_the_refusal_record_carries_the_decision_and_none_of_the_copy(
    platform: Platform, cast
) -> None:
    """A refusal's record is a decision and a count, not the refusal's text."""
    async with pipeline(platform) as answers, agent(
        answers, await principal_of(platform, cast.uploader)
    ) as running:
        state = await running.run("¿Cuánto gana mi compañero?")

    records = records_of(state)
    assert node_names(state) == ["classify", "refuse"]
    assert records[0]["decision"] == str(Intent.FORBIDDEN)
    assert records[1]["is_refusal"] is True
    assert records[1]["counts"] == {"message_chars": len(REFUSALS["salary_of_another"].text)}

    serialised = json.dumps(records, ensure_ascii=False)
    assert REFUSALS["salary_of_another"].es not in serialised
    assert "compañero" not in serialised, "the question leaked into a record"


# --- 1 & the scope boundary: the branches' tool sets --------------------------


#: The draft the pause/resume tests drive the branch with, and its arguments. Written out
#: rather than guessed by a lexical layer: ticket 40's branch takes the *call* from the state
#: (a model's function call from ticket 42), and a test that relied on a sentence being read
#: as "the third working day of November" would be testing a parser nobody promised.
DRAFT_CALL = "draft_leave_request"
DRAFT_ARGUMENTS = {
    "leave_type": "annual",
    "start_date": "2026-11-02",
    "end_date": "2026-11-04",
}


async def test_the_draft_branch_asks_what_to_draft_and_does_not_pause(
    platform: Platform, cast
) -> None:
    """待办操作 routes to the draft tools (ticket 40) and then to the human confirmation (41).

    The read-only half of the registry is ticket 39's and the draft half is ticket 40's, so
    what this test asserts is the branch's behaviour with nothing named: it **asks** rather
    than drafting — the ticket's own example 「帮我请下周三的假」 is a date a lexical layer
    cannot resolve and ticket 42's model call will — and, having asked a question, it does not
    pause waiting for confirmation of a form it never produced.
    """
    assert registered(ToolKind.READ_ONLY), "the read-only half is registered by ticket 39"
    assert registered(ToolKind.DRAFT), "the draft half is registered by ticket 40"

    actor = cast.uploader
    async with pipeline(platform) as answers, agent(
        answers, await principal_of(platform, actor)
    ) as running:
        state = await running.run(QUESTIONS[Intent.PENDING_ACTION])

    assert state["intent"] == str(Intent.PENDING_ACTION)
    assert state["tool_answer"]["message_key"] == "agent.draft.no_request"
    assert state["pending_action"]["status"] == "no_request"
    assert state["pending_action"]["tool"] is None
    assert state["prefill_form"] is None
    assert state["tool_outcome"] is None, "nothing ran, so no tool has an outcome"
    assert state["tools_registered"] == len(registered(ToolKind.DRAFT))
    assert node_names(state) == ["classify", "draft_tools", CONFIRMATION_NODE]
    assert "__interrupt__" not in state, "a question is not something to confirm"
    assert answers.model.calls == []


async def test_the_draft_branch_produces_a_form_and_pauses_for_the_human(
    platform: Platform, cast
) -> None:
    """The branch's real answer: a complete form in the payload, and a paused thread.

    §6.3's first requirement is that the `PrefillForm` is complete and editable, so this
    reads the payload's draft rather than a summary of it: every field the submission will
    write is there, with its label in both languages and the value the validation accepted.
    """
    await company_week(platform)
    actor = cast.uploader
    async with pipeline(platform) as answers, agent(
        answers, await principal_of(platform, actor)
    ) as running:
        state = await running.run(
            QUESTIONS[Intent.PENDING_ACTION],
            tool=DRAFT_CALL,
            arguments=DRAFT_ARGUMENTS,
        )

    assert state["pending_action"]["status"] == "proposed"
    assert state["tool"] == DRAFT_CALL
    assert state["tool_outcome"] == str(ToolOutcome.OK)
    form = state["prefill_form"]
    assert form["entity"] == "leave_request"
    assert [field["name"] for field in form["fields"]] == [
        "leave_type",
        "start_date",
        "end_date",
        "attachment_reference",
    ]
    assert form["submit_path"] == "/api/v1/leave/requests"
    assert state["agent_action_id"] is not None
    assert state["conversation_id"] is not None, "a draft belongs to a conversation"

    interrupts = state["__interrupt__"]
    assert len(interrupts) == 1, "a produced draft must pause exactly once"
    payload = interrupts[0].value
    assert payload["awaiting"] == "human_confirmation"
    assert payload["draft"] == form, "the payload must carry the form, not a summary of it"
    assert payload["agent_action_id"] == state["agent_action_id"]
    assert payload["expires_at"] == state["pending_action"]["expires_at"]
    assert answers.model.calls == [], "a draft is not a model call"


async def test_small_talk_gets_a_fixed_reply_and_no_model_call(platform: Platform, cast) -> None:
    """闲聊 is answered from a constant: a greeting is not worth a model that can invent one."""
    async with pipeline(platform) as answers, agent(
        answers, await principal_of(platform, cast.uploader)
    ) as running:
        state = await running.run(QUESTIONS[Intent.SMALL_TALK])

    assert state["intent"] == str(Intent.SMALL_TALK)
    assert state["notice"] == SMALL_TALK_REPLY
    assert node_names(state) == ["classify", "small_talk"]
    assert answers.model.calls == []
    assert await platform.scalar("SELECT count(*) FROM rag_conversations") == 0


# --- 4 & 5: the pause, the schema, and the restart ---------------------------


async def test_the_pause_is_written_to_the_langgraph_schema_and_not_to_redis(
    platform: Platform, cast, redis_client, libpq_dsn: str
) -> None:
    """Checklist: 中间状态使用 Postgres 检查点持久化在独立 schema 中，**不使用 Redis** 承载.

    Four facts, checked against the real database and the real Redis:

    * the DSN the checkpointer connects with points at this suite's database, as the
      *request* role, with `search_path` set to `langgraph`;
    * the schema exists by migration and holds the checkpoint tables, so the state has a
      home that an Alembic revision owns the name of;
    * the paused thread's checkpoints are **rows in that schema**, read back with plain SQL
      through libpq — not through the library that wrote them;
    * Redis holds no key that could be a checkpoint: it is the cache the design keeps for
      things that may be lost (§10.2), and this state may not be.
    """
    settings = get_settings()
    dsn = checkpoint_dsn(settings, test=True)
    assert f"options=-csearch_path%3D{CHECKPOINT_SCHEMA}" in dsn
    assert dsn.startswith(settings.runtime_test_database_url.replace("+psycopg", ""))
    assert "redis" not in dsn
    await company_week(platform)

    async with pipeline(platform) as answers, agent(
        answers, await principal_of(platform, cast.uploader)
    ) as running:
        thread = running.thread
        await running.run(
            QUESTIONS[Intent.PENDING_ACTION],
            tool=DRAFT_CALL,
            arguments=DRAFT_ARGUMENTS,
        )
        snapshot = await running.graph.aget_state(running.config)

    assert snapshot.next == (CONFIRMATION_NODE,), snapshot.next

    with psycopg.connect(libpq_dsn) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s",
                (CHECKPOINT_SCHEMA,),
            ).fetchall()
        }
        checkpoint_rows = connection.execute(
            f"SELECT count(*) FROM {CHECKPOINT_SCHEMA}.checkpoints WHERE thread_id = %s",
            (thread,),
        ).fetchone()[0]
    assert {
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "checkpoint_migrations",
    } <= tables, tables
    assert checkpoint_rows >= 1, "the pause was not written to langgraph.checkpoints"

    keys = [key async for key in redis_client.scan_iter(match="*")]
    assert not [key for key in keys if "checkpoint" in str(key) or thread in str(key)], keys


async def test_an_interrupted_run_resumes_on_a_graph_rebuilt_against_the_same_database(
    platform: Platform, cast
) -> None:
    """Checklist: 有测试验证：在一个中断的流程中重启服务后仍能继续.

    **What "restart" means here, precisely.** The first graph is compiled against one
    `AsyncPostgresSaver`, pauses, and then that saver's context manager is exited — its
    connection is closed and the object is gone. The second graph is a *different compiled
    object* on a *different* saver connected to the same database, given a *different*
    `AgentContext`, and it is invoked with `Command(resume=…)` and the same thread id and
    nothing else: no question, no state, no principal. Everything it knows comes from the
    `langgraph` schema.

    Two consequences a weaker test would miss, and both are asserted: the resumed state still
    holds the question and the pending action from before the pause, and the resumption
    value arrives at the paused node (`value_type` is the answer's *type*, because the value
    itself may be words a person typed and this graph records no content). Ticket 40 makes
    the pending action a *form*, so the resumed run also still knows which draft it was
    waiting about — the one fact a confirmation needs and cannot re-derive from the resume
    value.
    """
    principal = await principal_of(platform, cast.uploader)
    thread = uuid4().hex
    await company_week(platform)

    async with pipeline(platform) as first:
        async with open_checkpointer(get_settings(), test=True) as saver:
            paused = Agent(
                graph=build_graph(checkpointer=saver),
                context=_context(first, principal),
                thread=thread,
            )
            state = await paused.run(
                QUESTIONS[Intent.PENDING_ACTION],
                tool=DRAFT_CALL,
                arguments=DRAFT_ARGUMENTS,
            )
        # The first "process" is gone at this point: connection closed, saver dropped.
        assert state["__interrupt__"], "the draft branch must pause"
        form = state["prefill_form"]
        draft_id = state["agent_action_id"]

    async with pipeline(platform) as second:
        async with open_checkpointer(get_settings(), test=True) as saver:
            resumed = Agent(
                graph=build_graph(checkpointer=saver),
                context=_context(second, principal),
                thread=thread,
            )
            final = await resumed.run(resume={"action": "confirm"})
            snapshot = await resumed.graph.aget_state(resumed.config)

    assert snapshot.next == (), "the resumed run is still waiting"
    assert final["question"] == QUESTIONS[Intent.PENDING_ACTION], (
        "the resumed run lost the question, which only the checkpoint could have given it"
    )
    assert final["prefill_form"] == form, "the resumed run lost the form it was asking about"
    assert final["agent_action_id"] == draft_id
    assert final["pending_action"]["status"] == "proposed"
    assert final["confirmation"] == {
        "received": True,
        "value_type": "dict",
        "interpreted": False,
    }
    assert "__interrupt__" not in final, "the resumed run paused a second time"


def _context(answers: Pipeline, principal) -> AgentContext:  # noqa: ANN001
    """A fresh context per run, so a resumed run cannot be reading the first run's objects.

    The session is the pipeline's, as `agent()`'s is: the draft branch reads through it
    (ticket 40), and a context without one raises rather than drafting from nothing.
    """
    return AgentContext(
        principal=principal, answers=answers.service, session=answers.session
    )


async def test_resuming_does_not_re_run_the_nodes_before_the_pause(
    platform: Platform, cast
) -> None:
    """The other half of "resumes rather than starts over": the work before the pause is not redone.

    Asserted on the records, which are the state's own trace of what ran: `classify` and
    `draft_tools` appear once each across both runs. A graph that started over would have a
    second `classify` and a state rebuilt from the resume command — and, with the
    checkpointer swapped for an in-memory saver, it would not have the question at all,
    which is the mutation this pair of tests is written to catch.
    """
    principal = await principal_of(platform, cast.uploader)
    thread = uuid4().hex
    await company_week(platform)

    async with pipeline(platform) as first:
        async with open_checkpointer(get_settings(), test=True) as saver:
            paused = Agent(
                graph=build_graph(checkpointer=saver),
                context=_context(first, principal),
                thread=thread,
            )
            before = await paused.run(
                QUESTIONS[Intent.PENDING_ACTION],
                tool=DRAFT_CALL,
                arguments=DRAFT_ARGUMENTS,
            )
    assert node_names(before) == ["classify", "draft_tools"]

    async with pipeline(platform) as second:
        async with open_checkpointer(get_settings(), test=True) as saver:
            resumed = Agent(
                graph=build_graph(checkpointer=saver),
                context=_context(second, principal),
                thread=thread,
            )
            after = await resumed.run(resume="confirm")

    assert node_names(after) == ["classify", "draft_tools", CONFIRMATION_NODE]
    assert node_names(after).count("classify") == 1, "the classifier ran twice"
    assert after["intent"] == before["intent"]
    assert after["rule"] == before["rule"]
    assert [record["counts"] for record in records_of(after)][:2] == [
        record["counts"] for record in records_of(before)
    ]
    # The confirmation node ran exactly once, and it ran *after* the pause — its record's
    # `input_keys` say it read the pending action and the form the first run produced.
    assert records_of(after)[-1]["input_keys"] == ["pending_action", "prefill_form"]
    assert records_of(after)[-1]["counts"] == {
        "form_fields": len(before["prefill_form"])
    }


async def test_a_thread_with_no_checkpoint_cannot_be_resumed(platform: Platform, cast) -> None:
    """The negative control for the two tests above, on the same code path.

    A thread id that was never written has nothing to resume, and the run must fail rather
    than answer from an empty state. This is what makes the restart tests evidence: they
    would not pass against a graph whose state came from somewhere other than the database,
    because here there is no state to find.
    """
    principal = await principal_of(platform, cast.uploader)
    async with pipeline(platform) as answers, agent(
        answers, principal, thread=uuid4().hex
    ) as running:
        with pytest.raises(Exception) as raised:
            await running.run(resume="confirm")
    assert type(raised.value).__name__ in {"KeyError", "ValueError", "EmptyInputError"}, (
        raised.value
    )
