"""The graph's nodes: one decision, five branches, and the confirmation point.

DESIGN §6.1's shape, in this file:

    classify ─┬─ forbid   (D23, refused in code, no model call)
              ├─ answer   (the ticket 34 pipeline, streamed through unchanged)
              ├─ read     (ticket 39's read-only tools — registered: none)
              ├─ draft    (ticket 40's draft tools — registered: none) → confirm
              └─ small    (a fixed reply)

Each node is wrapped in `records.recorded`, which is what puts a content-free record of its
input names, output names, counts, decision and duration into the state. The wrapper is not
decoration: it is the mechanism behind the checklist line about records, and the only place
in this package that writes one.

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

**The two tool branches state that they have no tool.** They ask `app.ai.tools` what is
registered — today, nothing — and return `replies.NO_READ_ONLY_TOOL` / `NO_DRAFT_TOOL`, each
of which names the ticket that fills the branch. Ticket 40's `PrefillForm` and ticket 41's
confirm/reject handling are deliberately absent: `await_confirmation_node` pauses on
`interrupt()` with a payload that says what is missing, and on resume records only that *an*
answer arrived, by type, so that the human's words never become a record.
"""

from uuid import UUID

from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.ai.agents.intents import classify
from app.ai.agents.records import recorded
from app.ai.agents.replies import (
    CONFIRMATION_PENDING,
    NO_DRAFT_TOOL,
    NO_READ_ONLY_TOOL,
    SMALL_TALK_REPLY,
    refusal_for,
)
from app.ai.agents.state import AgentContext, AgentState
from app.ai.tools import ToolKind, registered
from app.domain.answer.models import AnswerEvent, EventKind


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
    reads=("question",),
    counts=(("tools_registered", "tools_registered"),),
)
async def read_only_tools_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    """只读数据查询: §6.2's read-only tools, which ticket 39 registers.

    The registry is asked rather than assumed, so the sentence this returns is a statement
    about the code and not a promise about it — and so that whatever ticket 39 adds is
    visible here the moment it is added.
    """
    return {
        "notice": NO_READ_ONLY_TOOL,
        "tools_registered": len(registered(ToolKind.READ_ONLY)),
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
