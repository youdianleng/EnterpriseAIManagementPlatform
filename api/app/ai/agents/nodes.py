"""The graph's nodes: one decision, five branches, and the confirmation point.

DESIGN §6.1's shape, in this file:

    classify ─┬─ forbid   (D23, refused in code, no model call)
              ├─ answer   (the ticket 34 pipeline, streamed through unchanged)
              ├─ read     (ticket 39's read-only tools — registered, called as the caller)
              ├─ draft    (ticket 40's draft tools — registered: none) → confirm
              └─ small    (a fixed reply)

Each node is wrapped in `records.recorded`, which is what puts a content-free record of its
input names, output names, counts, decision, tool name and duration into the state. The
wrapper is not decoration: it is the mechanism behind the checklist line about records, and
the only place in this package that writes one.

**The refusal branch constructs no model call, structurally.** `refuse_node` reads the
classifier's rule name and returns the bilingual constant for it. It does not touch
`runtime.context.answers`, and `runtime.context.answers` is the only route to a model that
a node has. That is why `tests/test_agent_graph.py` can assert `model.calls == []` — and it
also asserts that the run created no conversation row, which shows the ask never reached
the answer path at all rather than that the answer was discarded.

**The answer branch forwards, it does not collect.** See `state.py`: each `AnswerEvent` the
ticket 34 service yields is handed to `runtime.stream_writer` as it arrives, and the node
returns a summary. A node that accumulated the deltas and returned them would break §5.2's
first-token budget and would put a second copy of the answer text in the checkpoint.

**The read-only branch calls a tool, and the tool's values become the answer.**
`read_only_tools_node` resolves a name through the registry, runs it with the caller's
principal and the caller's period, and hands the *result* to `tools.render.render` — so the
figures in the answer are the figures the query returned and there is no step at which a
model could round, convert or invent one. A name that is not registered, or a question no
tool answers, produces the "no registered tool" reply with no figure in it at all; a query
that raises produces 「无法获取该数据」, likewise. Ticket 40's `PrefillForm` and ticket 41's
confirm/reject handling are deliberately absent: `await_confirmation_node` pauses on
`interrupt()` with a payload that says what is missing, and on resume records only that *an*
answer arrived, by type, so that the human's words never become a record.
"""

from datetime import UTC, date, datetime
from uuid import UUID

from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.ai.agents.intents import classify
from app.ai.agents.records import recorded
from app.ai.agents.replies import (
    SMALL_TALK_REPLY,
    confirmation_payload,
    refusal_for,
)
from app.ai.agents.state import AgentContext, AgentState
from app.ai.tools import (
    ToolCall,
    ToolContext,
    ToolKind,
    ToolOutcome,
    ToolResult,
    UnknownTool,
    arguments_for,
    invoke,
    lookup,
    registered,
    render,
    render_no_request,
    select_tool,
)
from app.config import get_settings
from app.domain.agent.models import PrefillForm
from app.domain.agent.service import RecordedDraft, service_for
from app.domain.answer.models import AnswerEvent, EventKind
from app.domain.attendance.business_day import madrid_today


@recorded(
    "classify",
    reads=("question",),
    # The question's *length*, never the question. A count is what makes a record worth
    # having for a node whose whole output is a decision.
    counts=(("question_chars", "question"),),
    decision_key="intent",
)
async def classify_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    """Which of the five the message is. No model, no I/O: see `intents.py`.

    The decision and the rule name are both constants from that module, which is what lets
    the record and the refusal copy quote a decision without quoting a question.
    """
    classification = classify(state["question"])
    return {"intent": str(classification.intent), "rule": classification.rule}


@recorded(
    "refuse",
    reads=("rule",),
    counts=(("message_chars", "refusal.text"),),
)
async def refuse_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    """D23's four prohibited asks, refused with a constant. **No model is reachable here.**

    `refusal_for` raises `NotAForbiddenRule` if the routing handed this node a rule that is
    not a prohibition, because a refusal that names the wrong reason is worse than a failed
    run: the person would be told "I cannot do that" for something the system can.
    """
    refusal = refusal_for(state["rule"])
    return {"refusal": refusal.as_dict(), "is_refusal": True, "notice": None}


@recorded(
    "answer_policy",
    reads=("question", "conversation_id"),
    counts=(
        ("deltas", "answer.delta_count"),
        ("citations", "answer.citation_count"),
    ),
    decision_key="answer.outcome",
)
async def answer_policy_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    """制度问答: hand the question to ticket 34's pipeline and forward what it yields.

    The service is called with the caller's principal, so §4.3's filter, the refusal D20
    requires, the citations and the persistence are all the ones production already uses.
    This node decides nothing about the answer; it relays the events and keeps the summary.
    """
    context = runtime.context
    writer = runtime.stream_writer
    summary: dict[str, object] = {
        "message_id": None,
        "conversation_id": state.get("conversation_id"),
        "model": None,
        "provider": None,
        "delta_count": 0,
        "citation_count": 0,
        "token_in": 0,
        "token_out": 0,
        "latency_ms": 0,
        "outcome": "unknown",
    }
    async for event in context.answers.stream(
        state["question"],
        context.principal,
        conversation_id=_conversation_id(state, context),
    ):
        # `writer` is the caller's pipe to the client: with `astream(stream_mode="custom")`
        # every event arrives as it is yielded, which is what keeps the first token early.
        writer(event)
        _summarise(event, summary)
    return {
        "answer": summary,
        # Written back because a *new* conversation's id is minted inside the answer path:
        # a resumed run — after an interrupt, or after a restart — needs it.
        "conversation_id": summary["conversation_id"],
        # The outcome, not a second flag: `done` carries `is_refusal` and the `refusal`
        # event precedes it, so "refused" is already the answer to the question.
        "is_refusal": summary["outcome"] == "refused",
    }


@recorded(
    "read_only_tools",
    reads=("question", "rule", "tool", "tool_arguments"),
    counts=(
        ("tools_registered", "tools_registered"),
        # How many fields the result carried — a count, never the result. §10.1 keeps
        # `tool_output` out of every record, and this is the record saying "there was one"
        # without saying what it was.
        ("result_fields", "tool_result"),
    ),
    decision_key="tool_outcome",
    tool_key="tool",
)
async def read_only_tools_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    """只读数据查询: DESIGN §6.2's read-only tools, **called as the caller**.

    Ticket 38 left this branch saying that no tool was registered. It now calls one, and
    the four facts that make the call safe are all visible from here:

    * **the identity is the context's.** Every implementation reads
      `runtime.context.principal`; nothing in this node, and no parameter in the
      registry's closed vocabulary, could name a second person.
    * **the name goes through the whitelist.** `invoke` looks the tool up in
      `app.ai.tools.REGISTRY`, so a name from the classifier, from a caller or from a
      model that is not registered raises `UnknownTool` and nothing runs.
    * **the period is the caller's too.** The lexical selector fills the defaults
      (this month, this year, all statuses) and a caller may override them with
      `tool_arguments`; a period is a date range, never a subject.
    * **the figures are the tool's.** The node renders the result
      (`tools.render.render`) rather than composing a sentence, so the numbers in
      `tool_answer` are the ones in `tool_result`, which are the ones the query returned.

    A question the classifier routed here that no tool answers — the caller's own
    payslip, say, which §6.2 has no tool for — is answered with the "no registered tool"
    reply rather than by guessing at a neighbour: a guess would read data nobody asked
    about, and the answer says no figure at all.

    **`UNKNOWN` names nothing, and that is deliberate.** `tool_name` is the one thing
    §10.1 lets a record say about a tool call, and `records.py`'s contract is that its
    value is a key of the registry. A name the model invented is neither, so when nothing
    ran this node stores `tool=None` and `tool_arguments={}` rather than the string it was
    handed — a trace field must not become a place model output can be written to.
    """
    context = runtime.context
    today = context.today or madrid_today(datetime.now(UTC))
    call = _tool_call(state, today)
    result = await _run(call, context, today)
    ran = result.outcome is not ToolOutcome.UNKNOWN
    return {
        "tool": result.tool if ran else None,
        "tool_arguments": dict(call.arguments) if ran and call is not None else {},
        "tool_result": dict(result.data) or None,
        "tool_answer": render(result).as_dict(),
        "tool_outcome": str(result.outcome),
        "tools_registered": len(registered(ToolKind.READ_ONLY)),
        "notice": None,
        "pending_action": None,
    }


@recorded(
    "draft_tools",
    reads=("question", "rule", "tool", "tool_arguments"),
    counts=(
        ("tools_registered", "tools_registered"),
        # How many keys the form carries — a count, never the form. §10.1 keeps
        # `tool_output` out of every record, and the form *is* the tool's output.
        ("form_fields", "prefill_form"),
    ),
    decision_key="tool_outcome",
    tool_key="tool",
)
async def draft_tools_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    """待办操作: §6.2's draft tools, called **as the caller**, producing a form — never a write.

    Four facts make this branch safe, and they are the same four the read-only branch states
    in its own docstring, with one substitution:

    * **the identity is the context's.** Each implementation reads
      `runtime.context.principal`; no parameter in the registry's closed vocabulary could
      name a second person, and the form has no identity field at all
      (`domain/agent/models.py::IDENTITY_FIELDS`).
    * **the name goes through the whitelist.** `invoke` looks the tool up in
      `app.ai.tools.REGISTRY`, so a name that is not registered runs nothing — and this
      node additionally refuses a name that is registered as *read-only*: the draft branch
      answers with a form, and running a query here would put a read under a draft's reply.
    * **the validation is the submission's.** The tools call the three `check_*` methods the
      write paths themselves call, so a draft cannot be refused afterwards for a rule
      nobody asked about.
    * **the tool writes nothing.** The row is this node's, through
      `domain/agent/service.py` — the platform records what the assistant proposed, which is
      DESIGN §6.3's fourth point and the reason the draft survives a restart.

    **The call is the seam ticket 42 fills.** Like the read-only branch, the node takes a
    name and its arguments from the state when the caller named one — that is where a
    model's function call arrives, and what a caller or a test uses to exercise one tool —
    and otherwise it answers 「dime qué borrador y para cuándo」: it does **not** guess dates.
    A lexical selector can reasonably decide "this month" (ticket 39's `selection.py` does),
    and it cannot reasonably decide that「下周三」is a specific date in 2026 — an invented
    date is a value in a form a person is asked to confirm, which is a different kind of
    guess from a default period in an answer.

    **A run with nothing to confirm does not pause.** `await_confirmation` is reached either
    way — the topology is ticket 38's and does not change — and it interrupts only when a
    form came out of this node. A branch that paused on a question it had just asked would
    be waiting for an answer to something it never showed.
    """
    context = runtime.context
    today = context.today or madrid_today(datetime.now(UTC))
    call = _draft_call(state)
    result = await _run_draft(call, context, today) if call is not None else _no_call()

    drafted: RecordedDraft | None = None
    if result.outcome is ToolOutcome.OK:
        drafted = await _record_draft(result, call, state, context, runtime)  # type: ignore[arg-type]

    ran = result.outcome is not ToolOutcome.UNKNOWN
    answer = (
        render(result).as_dict() if call is not None else render_no_request().as_dict()
    )
    return {
        "tool": result.tool if ran else None,
        "tool_arguments": dict(call.arguments) if ran and call is not None else {},
        "tool_result": dict(result.data) or None,
        "tool_answer": answer,
        "tool_outcome": str(result.outcome) if call is not None else None,
        "prefill_form": dict(result.data) if drafted is not None else None,
        "agent_action_id": None if drafted is None else str(drafted.action.id),
        "pending_action": {
            "status": _draft_status(result, drafted, call),
            "tool": result.tool if ran else None,
            "draft_id": None if drafted is None else str(drafted.action.id),
            "expires_at": (
                None if drafted is None else drafted.action.expires_at.isoformat()
            ),
        },
        # Written back because the draft's conversation may be one this node minted: a
        # resumed run, or a second question in the same thread, needs the id.
        "conversation_id": (
            str(drafted.conversation_id)
            if drafted is not None
            else state.get("conversation_id")
        ),
        "tools_registered": len(registered(ToolKind.DRAFT)),
        "notice": answer["text"] if call is None else None,
    }


@recorded(
    "await_confirmation",
    reads=("pending_action", "prefill_form"),
    counts=(("form_fields", "prefill_form"),),
)
async def await_confirmation_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    """The pause DESIGN §6.1 draws, and the reason this graph is not just a router.

    `interrupt()` stops the run **and the checkpointer stores it**, so the thread is
    resumable by a different process tomorrow (§6.3's 「用户关掉浏览器第二天回来确认」). The
    payload carries the **complete form** — §6.3's first requirement — so a client can draw
    every field the submission will write, with its expiry, from the checkpoint alone.

    **Ticket 41's half is the confirmation itself.** What happens with the answer the human
    gives — re-validating, submitting as the employee, recording `confirmed_at` and the
    resulting entity — is that ticket's; nothing here interprets the resume value beyond
    recording its *type*, because a person's words must not become a record and inventing a
    partial confirmation now would be a decision this ticket has no evidence to make.

    **Nothing to confirm means no pause.** The node is reached from the draft branch either
    way; when the branch answered a question instead of drafting (no call was named) there
    is no form, and `interrupt()` on it would park a run that has nothing to wait for.
    """
    pending = state.get("pending_action") or {}
    if pending.get("status") != "proposed":
        return {
            "confirmation": {
                "received": False,
                "value_type": "none",
                "interpreted": False,
            }
        }

    answer = interrupt(
        confirmation_payload(
            draft=state.get("prefill_form"),
            draft_id=state.get("agent_action_id"),
            expires_at=pending.get("expires_at"),
        )
    )
    return {
        "confirmation": {
            "received": True,
            "value_type": type(answer).__name__,
            "interpreted": False,
        }
    }


@recorded(
    "small_talk",
    reads=("question",),
    counts=(("question_chars", "question"),),
)
async def small_talk_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    """闲聊: a fixed reply. A greeting is not worth a model call, and cannot then invent one."""
    return {"notice": SMALL_TALK_REPLY, "is_refusal": False}


# --- internals ----------------------------------------------------------------


def _tool_call(state: AgentState, today: date) -> ToolCall | None:
    """The call this run makes: a name the caller gave, or the one the question selects.

    A caller may name a tool and its arguments (`state["tool"]` / `state["tool_arguments"]`)
    — that is the seam a model's function call arrives through in ticket 42, and what a
    test uses to exercise one tool without writing a sentence the lexical selector would
    have to happen to read. Otherwise `selection.select_tool` decides, and `None` means
    the question is a data question no registered tool answers.

    An arguments mapping a *named* tool cannot use returns `None` too, rather than an empty
    one: `get_colleague_contact` without a name would otherwise be a directory dump.
    """
    named = state.get("tool")
    if not named:
        return select_tool(state["question"], today=today)
    arguments = state.get("tool_arguments") or arguments_for(
        named, state["question"], today=today
    )
    if arguments is None:
        return None
    return ToolCall(name=named, arguments=arguments)


async def _run(
    call: ToolCall | None, context: AgentContext, today: date
) -> ToolResult:
    """Run the call, or state that there was nothing to run.

    Three ways to end up with no result, and they are one outcome because they are one
    fact — nothing was read: no tool was selected, the named tool is not registered
    (the whitelist's refusal, which is why `UnknownTool` is caught here and answered
    rather than propagated), or the context carries no session, which is a wiring bug
    and raises rather than pretending to be a refusal.
    """
    if call is None:
        return ToolResult(tool="", outcome=ToolOutcome.UNKNOWN)
    if context.session is None:
        raise RuntimeError(
            f"{call.name} needs a session: the tool branches read through "
            "AgentContext.session, and a context built without one cannot read the "
            "balance, the week or the punch a tool is asked about"
        )
    context_for_tools = ToolContext(
        principal=context.principal, session=context.session, today=today
    )
    try:
        return await invoke(call, context_for_tools)
    except UnknownTool:
        return ToolResult(tool=call.name, outcome=ToolOutcome.UNKNOWN)


def _draft_call(state: AgentState) -> ToolCall | None:
    """The draft this run produces, or `None` — which is an answer, not a failure.

    A *named* tool with its arguments is the seam ticket 42 replaces with a model's function
    call, and what a caller or a test uses to exercise one draft without writing a sentence
    a lexical selector would have to happen to read. `None` means nothing named one, and the
    node answers with a question rather than inventing a call: see its docstring for why
    guessing a date is not the same kind of guess as defaulting a period.
    """
    named = state.get("tool")
    if not named:
        return None
    return ToolCall(name=named, arguments=dict(state.get("tool_arguments") or {}))


def _no_call() -> ToolResult:
    """Nothing was named. `UNKNOWN` is the outcome that states nothing ran."""
    return ToolResult(tool="", outcome=ToolOutcome.UNKNOWN)


async def _run_draft(
    call: ToolCall, context: AgentContext, today: date
) -> ToolResult:
    """Run one *draft* tool, and refuse a registered name that is not one.

    The kind check is the draft branch's own whitelist: a name the model produced that is
    registered as read-only would otherwise run a *query* here, and its values would arrive
    under the draft branch's reply with no form beside them. Answering "no such draft tool"
    is the same shape as the whitelist's answer for a name nobody registered.
    """
    try:
        tool = lookup(call.name)
    except UnknownTool:
        return ToolResult(tool=call.name, outcome=ToolOutcome.UNKNOWN)
    if tool.kind is not ToolKind.DRAFT:
        return ToolResult(tool=call.name, outcome=ToolOutcome.UNKNOWN)
    return await _run(call, context, today)


async def _record_draft(
    result: ToolResult,
    call: ToolCall | None,
    state: AgentState,
    context: AgentContext,
    runtime: Runtime[AgentContext],
) -> RecordedDraft:
    """Record the proposed draft, and hand it to the platform's own table.

    The form is re-read through `PrefillForm.from_stored` rather than trusted as the dict
    the tool built: the row is the record and this is the contract, so a shape that drifted
    fails here, in the installation, rather than in a browser that cannot draw a field.

    `thread_id` comes from the run's execution info — the LangGraph thread, which for this
    system is the conversation (§3.6 keeps both) — and is `None` for a graph compiled
    without a checkpointer, which is a real state rather than a missing value.
    """
    assert context.session is not None  # `_run` refused a context without one
    assert call is not None
    form = PrefillForm.from_stored(result.data)
    assert form is not None  # `OK` from a draft tool is exactly "there is a form"
    service = service_for(
        context.session, ttl_hours=get_settings().agent_draft_ttl_hours
    )
    return await service.record_draft(
        principal=context.principal,
        conversation_id=_conversation_id(state, context),
        question=state["question"],
        tool_name=call.name,
        tool_input=dict(call.arguments),
        tool_output=dict(result.data),
        form=form,
        thread_id=_thread_id(runtime),
    )


def _thread_id(runtime: Runtime[AgentContext]) -> str | None:
    """The LangGraph thread this run belongs to, or `None` without a checkpointer."""
    info = getattr(runtime, "execution_info", None)
    thread = getattr(info, "thread_id", None)
    return None if thread is None else str(thread)


def _draft_status(
    result: ToolResult, drafted: RecordedDraft | None, call: ToolCall | None
) -> str:
    """What the draft branch decided, as one word a client and a test can read.

    `proposed` is the only value that pauses: it means a form exists and a human is being
    asked about it. The three others are answers — a refusal from the kernel, a draft the
    contents would not allow, and "tell me what to draft" — and none of them has anything
    to confirm.
    """
    if drafted is not None:
        return "proposed"
    if call is None:
        return "no_request"
    if result.outcome is ToolOutcome.INVALID:
        return "invalid"
    if result.outcome is ToolOutcome.REFUSED:
        return "refused"
    return "no_draft"


def _conversation_id(state: AgentState, context: AgentContext) -> UUID | None:
    """The conversation to continue: the caller's, or the one an earlier run created.

    The second half is what a resumed thread needs — a run that paused waiting for
    confirmation must finish the same conversation, and after a restart the only place that
    id exists is the checkpoint.
    """
    if context.conversation_id is not None:
        return context.conversation_id
    stored = state.get("conversation_id")
    return UUID(stored) if stored else None


def _summarise(event: AnswerEvent, summary: dict[str, object]) -> None:
    """Fold one event into the summary. Counts and names; never text or citations."""
    if event.kind is EventKind.START:
        summary["message_id"] = event.data.get("message_id")
        summary["conversation_id"] = event.data.get("conversation_id")
        summary["model"] = event.data.get("model")
        summary["provider"] = event.data.get("provider")
    elif event.kind is EventKind.CITATIONS:
        summary["citation_count"] = len(event.data.get("citations") or ())
    elif event.kind is EventKind.DELTA:
        summary["delta_count"] = int(summary["delta_count"]) + 1
    elif event.kind is EventKind.REFUSAL:
        summary["outcome"] = "refused"
    elif event.kind is EventKind.ERROR:
        # Terminal: the driver yields no `done` after a model failure (ticket 34).
        summary["outcome"] = "failed"
        summary["error_code"] = event.data.get("code")
    elif event.kind is EventKind.DONE:
        summary["token_in"] = event.data.get("token_in", 0)
        summary["token_out"] = event.data.get("token_out", 0)
        summary["latency_ms"] = event.data.get("latency_ms", 0)
        if summary["outcome"] == "unknown":
            summary["outcome"] = "refused" if event.data.get("is_refusal") else "answered"


__all__ = [
    "answer_policy_node",
    "await_confirmation_node",
    "classify_node",
    "draft_tools_node",
    "read_only_tools_node",
    "refuse_node",
    "small_talk_node",
]
