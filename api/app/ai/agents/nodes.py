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
    CONFIRMATION_PENDING,
    NO_DRAFT_TOOL,
    SMALL_TALK_REPLY,
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
    registered,
    render,
    select_tool,
)
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
    reads=("question",),
    counts=(("tools_registered", "tools_registered"),),
)
async def draft_tools_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    """待办操作: §6.2's draft tools, which ticket 40 registers.

    `pending_action` is the seam ticket 40 fills with the tool's output and ticket 41 with
    the `PrefillForm`. Today it says plainly that there is nothing to confirm.
    """
    return {
        "notice": NO_DRAFT_TOOL,
        "tools_registered": len(registered(ToolKind.DRAFT)),
        "pending_action": {"status": "no_tool_registered", "tool": None},
    }


@recorded(
    "await_confirmation",
    reads=("pending_action",),
    counts=(("pending_fields", "pending_action"),),
)
async def await_confirmation_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    """The pause DESIGN §6.1 draws, and the reason this ticket is not just a router.

    `interrupt()` stops the run **and the checkpointer stores it**, so the thread is
    resumable by a different process tomorrow (§6.3's 「用户关掉浏览器第二天回来确认」). The
    payload is a placeholder: §6.3 requires a complete editable `PrefillForm` and an
    explicit confirmation click, which is ticket 40's form and ticket 41's handling.

    **What is recorded on resume is the answer's *type*, never its content.** A resume value
    can be anything a caller sends, including a sentence a person typed, and the whole point
    of this module is that a person's words do not become a record. Nothing here interprets
    the value either — that is ticket 41's confirm/reject branch, and inventing a partial
    version of it here would be a decision this ticket has no evidence to make.
    """
    answer = interrupt(CONFIRMATION_PENDING)
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
            f"{call.name} needs a session: the read-only branch reads through "
            "AgentContext.session, and a context built without one cannot answer a "
            "data question"
        )
    context_for_tools = ToolContext(
        principal=context.principal, session=context.session, today=today
    )
    try:
        return await invoke(call, context_for_tools)
    except UnknownTool:
        return ToolResult(tool=call.name, outcome=ToolOutcome.UNKNOWN)


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
