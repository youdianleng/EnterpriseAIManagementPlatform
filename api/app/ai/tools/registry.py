"""The tool registry: DESIGN §6.2's whitelist, and the only door to an implementation.

**A name maps to an implementation here, and nowhere else.** `REGISTRY` is a
`MappingProxyType` of `Tool` values keyed by the same name the tool carries, and
`lookup` is the single function that turns a name into one. That is what makes the
checklist's 「工具清单是白名单，未注册的工具无法被模型调用」 a property of the code:
a node that wants to run a tool has a *name* — from the classifier today, from a
model's function call in ticket 42 — and a name that is not a key of this mapping
raises `UnknownTool` rather than falling back to anything. A lookup that defaulted
would answer a question nobody asked with somebody else's data.

`invoke` is the other half: it looks the tool up and then **states** a failure. A tool
that raises does not return, and `agents/records.py` records a node failure as a log
line rather than as state — so a query that raised would leave the run with no result
at all. Catching here turns it into `ToolOutcome.FAILED`, whose answer is 「无法获取该
数据」 with no figure in it; the exception's class name travels and its message does
not, for the reason the node records give.

**The registry has no write kind, and that is the constraint's first layer.** D22 and
`codebase-design.md` §6's constraint B say the agent never writes the database, and
layer one of the three defences is structural: `ai/**` reaches no write repository, and
the tool set contains no write tool. `ToolKind` therefore has exactly two members —
`READ_ONLY` and `DRAFT` — and a tool that wrote could not be described by this type at
all. Making the forbidden thing unrepresentable is worth more than a rule saying not
to do it, which is the whole reason §6 records three layers instead of one.

**The read-only half is registered here; the draft half is ticket 40's.** §8's table
gives constraint B's structural layer to ticket 40, and the assertion this ticket
establishes is the one that has to hold for *every* tool: no registered implementation
reaches a write. `tests/test_agent_readonly_tools.py` walks each implementation's own
source with `ast` and fails on a write-shaped call or a write-shaped SQL literal, with
a positive control that proves the walker catches what it looks for. Ticket 40 extends
that walk to its draft tools; the walk itself is here.

**`search_policy` is deliberately not a tool.** §6.2 lists it as §6.2's sixth read-only
row and `agents/intents.py` routes 制度问答 to `answer_policy`, which streams ticket
34's own pipeline: retrieval filtered by §4.3, the D20 refusal, the citations, the
persistence. Registering a second entry point to that retrieval would be a second path
to exactly the thing ticket 35 pinned — and a tool returns structured values, which an
answer with citations is not. The ticket's own wording allows this: 「如果把它作为工具暴
露」. One path, and it is the one ticket 34 built.

**A tool's inputs and outputs are never recorded.** §10.1 lists `tool_input` and
`tool_output` among the fields that must never leave the installation, so the record of
a tool's execution is its *name*, its duration and its outcome (`agents/records.py`),
never its arguments or its result. The result does travel in the graph's state, which is
the installation's own Postgres and the only place the caller's own figures belong.
"""

import time
from types import MappingProxyType
from typing import Final

from app.ai.tools.models import (
    Tool,
    ToolCall,
    ToolContext,
    ToolKind,
    ToolOutcome,
    ToolResult,
    UnknownTool,
)
from app.ai.tools.readonly import READ_ONLY_TOOLS
from app.logging import get_logger

logger = get_logger(__name__)

#: Every registered tool, keyed by name. Ticket 40 merges the draft half into this
#: literal; nothing else may add a tool, which is what makes "who can register a tool"
#: have exactly one answer — an edit here, in the ticket that owns it.
REGISTRY: Final[MappingProxyType[str, Tool]] = MappingProxyType({**READ_ONLY_TOOLS})


def registered(kind: ToolKind | None = None) -> tuple[Tool, ...]:
    """The registered tools, optionally of one kind, in name order.

    Name order rather than insertion order, because a caller that picks "the first tool"
    should get the same one whatever order the literal above happens to be written in.
    """
    tools = (tool for tool in REGISTRY.values() if kind is None or tool.kind is kind)
    return tuple(sorted(tools, key=lambda tool: tool.name))


def lookup(name: str) -> Tool:
    """The tool a name refers to, or `UnknownTool`. See the module docstring."""
    try:
        return REGISTRY[name]
    except KeyError as error:
        raise UnknownTool(
            f"{name!r} is not a registered tool "
            f"({', '.join(sorted(REGISTRY))}); an unregistered name is refused rather "
            "than answered with another tool's data"
        ) from error


async def invoke(call: ToolCall, context: ToolContext) -> ToolResult:
    """Run one tool as `context.principal`, stating any failure as an outcome.

    `UnknownTool` is deliberately **not** caught: it is the whitelist's answer, and the
    caller is what turns it into the "no such tool" reply — a tool that does not exist
    has no result to state.
    """
    tool = lookup(call.name)
    started = time.perf_counter()
    try:
        result = await tool.run(call, context)
    except Exception as error:  # noqa: BLE001 - a failed query is an outcome, not a crash
        logger.warning(
            "agent_tool_failed",
            tool_name=call.name,
            error_type=type(error).__name__,
            latency_ms=int((time.perf_counter() - started) * 1000),
            # Deliberately not `str(error)`: a query's exception can quote an argument.
        )
        return ToolResult(
            tool=call.name, outcome=ToolOutcome.FAILED, error_type=type(error).__name__
        )
    logger.info(
        "agent_tool_ran",
        tool_name=call.name,
        outcome=str(result.outcome),
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return result


__all__ = ["REGISTRY", "Tool", "ToolKind", "invoke", "lookup", "registered"]
