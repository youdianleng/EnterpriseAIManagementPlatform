"""The topology as code: one entry node, five branches, one pause (DESIGN §6.1).

The checklist line is 「图的拓扑以代码表达且可被单测直接调用（不需要走 HTTP）」, so the graph is
built by a function, the routing is a mapping a test can read, and there is no route in
`app/api/` for any of it. Ticket 41 is where a request reaches this graph; until then the
only callers are tests, and adding an endpoint now would add a permission-matrix row for a
surface nobody uses.

**The routing table is data, and it is the thing tests assert.** `ROUTES` maps each of the
five intents to the node that handles it, `branch_of` is the router LangGraph calls
(a one-line lookup, so there is one place a decision becomes a destination), and
`build_graph` registers the conditional edges with the branch names themselves as the path
map — the decision's value *is* the node's name.

**A wrong destination in that table fails a test, and the reason is worth knowing.**
`add_conditional_edges` does **not** check the names its router returns: measured on
langgraph 1.2.12, a router that returns an unregistered node compiles fine and, at run time,
logs "wrote to unknown channel branch:to:<name>, ignoring it" and silently ends the run.
So "the table is right" is enforced by `tests/test_agent_graph.py`, which asserts every
value in `ROUTES` is a key of `NODES` and that the compiled graph's nodes are exactly
`NODES` — the same shape as this repository's other structural constraints. `branch_for`
covers the other half: an intent with no row raises `UnroutableIntent` rather than
defaulting.

    Intent.FORBIDDEN          → refuse            (D23: refused in code, no model call)
    Intent.POLICY_QUESTION    → answer_policy     (ticket 34's pipeline, forwarded)
    Intent.READ_ONLY_QUERY    → read_only_tools   (ticket 39's tools, called as the caller)
    Intent.PENDING_ACTION     → draft_tools       (placeholder: ticket 40) → await_confirmation
    Intent.SMALL_TALK         → small_talk        (a fixed reply)

**Where the checkpointing is.** `build_graph` takes a checkpointer and passes it to
`compile`. With one, every step is written to the `langgraph` schema of the same Postgres
(`checkpoint.py`), and `interrupt()` inside `await_confirmation` makes the run resumable by
a *different* process with the same thread id and database — which is DESIGN §6.1's whole
argument for choosing LangGraph, and `tests/test_agent_graph.py`'s restart test. With
`None`, the graph runs in memory and an interrupt cannot be resumed across instances; that
is the mutation the restart test is written to catch.

**The thread id names the interrupted flow.** `thread_config` writes the one config key
LangGraph requires, and the caller decides what a thread is: for this system it is the
conversation, so that a second HTTP request about the same conversation finds the paused
run. Ticket 41 passes the conversation's id; this ticket only makes the mapping explicit so
that the restart test can use it.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RunnableConfig

from app.ai.agents.intents import Intent
from app.ai.agents.nodes import (
    answer_policy_node,
    await_confirmation_node,
    classify_node,
    draft_tools_node,
    read_only_tools_node,
    refuse_node,
    small_talk_node,
)
from app.ai.agents.records import Node
from app.ai.agents.state import AgentContext, AgentState

#: The graph's entry point. Every run classifies first, and no other node is ever the entry.
ENTRY_NODE: Final[str] = "classify"

#: The pause. `interrupt()` inside it is what makes the run resumable; see `nodes.py`.
CONFIRMATION_NODE: Final[str] = "await_confirmation"

#: The node every node is registered from. One mapping rather than seven `add_node` calls,
#: so "what nodes exist" is a value a test can read without compiling anything.
NODES: Final[Mapping[str, Node]] = MappingProxyType(
    {
        ENTRY_NODE: classify_node,
        "refuse": refuse_node,
        "answer_policy": answer_policy_node,
        "read_only_tools": read_only_tools_node,
        "draft_tools": draft_tools_node,
        CONFIRMATION_NODE: await_confirmation_node,
        "small_talk": small_talk_node,
    }
)

#: The branches that end the run where they are.
TERMINAL_BRANCHES: Final[tuple[str, ...]] = (
    "refuse",
    "answer_policy",
    "read_only_tools",
    "small_talk",
)

#: The one branch that does not: a draft is what a human has to confirm (DESIGN §6.3).
DRAFT_BRANCH: Final[str] = "draft_tools"

#: Intent → node. The whole routing decision, as data.
ROUTES: Final[Mapping[Intent, str]] = MappingProxyType(
    {
        Intent.FORBIDDEN: "refuse",
        Intent.POLICY_QUESTION: "answer_policy",
        Intent.READ_ONLY_QUERY: "read_only_tools",
        Intent.PENDING_ACTION: DRAFT_BRANCH,
        Intent.SMALL_TALK: "small_talk",
    }
)


class UnroutableIntent(LookupError):
    """The state carries an intent this graph has no branch for.

    Two causes, and both are bugs rather than inputs: `classify` returned a sixth outcome
    (or a value that is not an `Intent` at all), or an intent exists in `intents.py` with no
    row here. Raised in both cases rather than defaulted — sending the question down
    somebody else's branch would hide the disagreement until an answer looked wrong for no
    visible reason.
    """


def branch_for(intent: Intent) -> str:
    """The node an intent goes to. The decision, as a value → a value."""
    try:
        return ROUTES[intent]
    except KeyError as error:
        raise UnroutableIntent(
            f"{intent!r} has no branch; ROUTES covers {sorted(str(i) for i in ROUTES)}. "
            "Adding an intent to `intents.py` means adding a node and a row here."
        ) from error


def branch_of(state: AgentState) -> str:
    """The router LangGraph calls after `classify`. One lookup, no logic.

    The state carries the intent as a string because the checkpointer stores JSON, so this
    is also where a value that is not one of the five is caught — see `UnroutableIntent`.
    """
    raw = state["intent"]
    try:
        intent = Intent(raw)
    except ValueError as error:
        raise UnroutableIntent(
            f"the state's intent {raw!r} is not one of the five outcomes "
            f"({sorted(str(i) for i in Intent)})"
        ) from error
    return branch_for(intent)


def build_graph(
    *, checkpointer: BaseCheckpointSaver | None = None
) -> CompiledStateGraph:
    """The compiled graph, with or without persistence.

    `checkpointer` is the argument that matters: the Postgres saver from `checkpoint.py`
    makes an interrupted run survive the process, and `None` is the in-memory case the
    restart test uses as its mutation — a graph rebuilt without one cannot resume, because
    the only record of the pause died with the first instance.
    """
    graph: StateGraph = StateGraph(AgentState, context_schema=AgentContext)
    for name, node in NODES.items():
        graph.add_node(name, node)

    graph.add_edge(START, ENTRY_NODE)
    # The path map's values are the branch names themselves: a decision is a destination,
    # so `branch_for` needs no translation table of its own and a new node cannot be
    # registered without a route.
    graph.add_conditional_edges(ENTRY_NODE, branch_of)
    for name in TERMINAL_BRANCHES:
        graph.add_edge(name, END)
    graph.add_edge(DRAFT_BRANCH, CONFIRMATION_NODE)
    graph.add_edge(CONFIRMATION_NODE, END)

    return graph.compile(checkpointer=checkpointer)


def thread_config(thread_id: str) -> RunnableConfig:
    """The config that names a resumable run. See the module docstring for what a thread is.

    A function rather than a dict literal in every caller, because the key is
    `configurable.thread_id` — three levels of string that a typo turns into a run that
    silently starts over.
    """
    return {"configurable": {"thread_id": thread_id}}


def nodes_of(graph: CompiledStateGraph) -> set[str]:
    """The node names a compiled graph actually has, for a test that wants the topology."""
    return {str(name) for name in graph.get_graph().nodes if not str(name).startswith("__")}


__all__ = [
    "CONFIRMATION_NODE",
    "DRAFT_BRANCH",
    "ENTRY_NODE",
    "NODES",
    "ROUTES",
    "TERMINAL_BRANCHES",
    "UnroutableIntent",
    "branch_for",
    "branch_of",
    "build_graph",
    "nodes_of",
    "thread_config",
]
