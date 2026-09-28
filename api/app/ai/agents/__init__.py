"""The graph, its nodes, its routes and its checkpointer — DESIGN §6.1 in this tree.

`build_graph()` is the way in; `ROUTES` is the routing table; `checkpoint.open_checkpointer`
is what makes an interrupted run survive a restart. Nothing here is reachable over HTTP yet:
a graph that only tests call needs no route, and ticket 41 is where a request reaches it.
"""

from app.ai.agents.checkpoint import (
    CHECKPOINT_SCHEMA,
    checkpoint_dsn,
    open_checkpointer,
)
from app.ai.agents.graph import (
    CONFIRMATION_NODE,
    DRAFT_BRANCH,
    ENTRY_NODE,
    NODES,
    ROUTES,
    TERMINAL_BRANCHES,
    UnroutableIntent,
    branch_for,
    branch_of,
    build_graph,
    nodes_of,
    thread_config,
)
from app.ai.agents.intents import (
    DEFAULT_INTENT,
    DEFAULT_RULE,
    FORBIDDEN_RULES,
    RULES,
    Classification,
    Intent,
    Rule,
    classify,
)
from app.ai.agents.records import ALLOWED_FIELDS, NodeRecord, node_names, records_of
from app.ai.agents.replies import (
    NO_DRAFT_TOOL,
    NO_READ_ONLY_TOOL,
    REFUSALS,
    SMALL_TALK_REPLY,
    NotAForbiddenRule,
    Refusal,
    refusal_for,
)
from app.ai.agents.state import AgentContext, AgentState

__all__ = [
    "ALLOWED_FIELDS",
    "CHECKPOINT_SCHEMA",
    "CONFIRMATION_NODE",
    "DEFAULT_INTENT",
    "DEFAULT_RULE",
    "DRAFT_BRANCH",
    "ENTRY_NODE",
    "FORBIDDEN_RULES",
    "NODES",
    "NO_DRAFT_TOOL",
    "NO_READ_ONLY_TOOL",
    "REFUSALS",
    "ROUTES",
    "RULES",
    "SMALL_TALK_REPLY",
    "TERMINAL_BRANCHES",
    "AgentContext",
    "AgentState",
    "Classification",
    "Intent",
    "NodeRecord",
    "NotAForbiddenRule",
    "Refusal",
    "Rule",
    "UnroutableIntent",
    "branch_for",
    "branch_of",
    "build_graph",
    "checkpoint_dsn",
    "classify",
    "node_names",
    "nodes_of",
    "open_checkpointer",
    "records_of",
    "refusal_for",
    "thread_config",
]
