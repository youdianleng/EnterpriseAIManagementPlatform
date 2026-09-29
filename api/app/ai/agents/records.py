"""What a node records about itself: names, counts, timings, the decision, the error type.

The checklist line is 「每个节点的输入输出被记录，但**不含**对话正文与检索内容」, and DESIGN §10.1
gives the reason it has to be that shape: the agent's traces are allowed to leave the
installation (option (A), LangSmith Cloud with PII redaction) only if what leaves carries
`node_name`, `latency_ms`, token counts, `retrieval_hit_count`, `error_type` and
`is_refusal` — and **never** `content`, `messages`, `prompt`, `query`, `chunk_text`,
`citations`, `tool_input` or `tool_output`. Ticket 42 writes the filter that enforces that
list at the provider boundary. This module is the other half: the record is *constructed*
from safe material, so a filter has almost nothing to remove.

**How a record can be trusted rather than merely reviewed.** `recorded()` builds the record
out of five things and nothing else:

1. the node's declared **input key names** (`reads=`), which are literals in the decorator;
2. the node's returned **output key names**, taken from the update dict's keys — names, not
   values;
3. **counts**, from a declared `(label, state key)` list, reduced with `len()` or taken
   as an integer;
4. the **decision**, read from one declared state key, which by contract holds an enum
   value such as `Intent.FORBIDDEN` — a constant, not a string a person typed;
5. the **tool name** (ticket 39), read from one declared state key, which by contract holds
   a key of the tool registry — again a constant. §10.1 lists `tool_name` among the fields
   a trace may carry and `tool_input`/`tool_output` among the fields it may not, so what a
   tool call leaves behind is *which* tool ran and never what it was asked or returned.

A node therefore cannot leak a passage into its record by accident: there is no code path
that copies a value into the record. The two places a value *is* read — the decision and
the tool name — are keys whose values the graph's own nodes set from an enum and from a
registry.

**Why the failure record is a log line and not state.** A node that raises does not return,
so its state update is never applied and the checkpointer stores nothing: there is no
"output" to attach a record to. The wrapper writes the record to the structured log with
`error_type` and re-raises, which is the only place a failure record can exist. The
exception's own message is deliberately **not** copied into it — an exception raised inside
the answer path can quote the prompt — and the caller logs the traceback where a human can
read it.

**`GraphBubbleUp` is not a failure.** `interrupt()` pauses a node by raising, and so does a
`Command` that bubbles to a parent graph. Recording either as an error would put a spurious
`error_type` on every paused confirmation, which is the one state this ticket exists to
persist.
"""

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import wraps
from typing import Any

from langgraph.errors import GraphBubbleUp
from langgraph.runtime import Runtime

from app.logging import get_logger

logger = get_logger(__name__)

#: The state key the records accumulate under. Named here so `records_of` and the decorator
#: agree without either importing the other's caller.
RECORD_KEY = "records"

#: The fields a record may carry, and the whole of what ticket 42's filter may pass on.
#: Written out so a reader comparing this module with DESIGN §10.1 can see that the set is
#: a subset of `ALLOWED_TRACE_FIELDS` and shares none of `FORBIDDEN`. `tool_name` joined
#: the set in ticket 39, when the read-only branch began calling tools: it is the one
#: thing §10.1 lets a trace say about a tool call.
ALLOWED_FIELDS: tuple[str, ...] = (
    "node_name",
    "tool_name",
    "decision",
    "input_keys",
    "output_keys",
    "counts",
    "latency_ms",
    "is_refusal",
    "error_type",
)


@dataclass(frozen=True, slots=True)
class NodeRecord:
    """One node execution, as a record of *shape* rather than of content.

    `input_keys` and `output_keys` are the state fields the node read and wrote, and the
    field names are the whole of it — `{"question"}` says a node read the question without
    saying what it was. `counts` carries `question_chars: 41` for the same reason: a
    length is an operational fact, and the text is not.

    `tool_name` is the registry key a tool-calling node used, or `None`; never the call's
    arguments and never its result. See the module docstring.
    """

    node_name: str
    tool_name: str | None = None
    decision: str | None = None
    input_keys: tuple[str, ...] = ()
    output_keys: tuple[str, ...] = ()
    counts: Mapping[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    is_refusal: bool = False
    error_type: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable, because the checkpointer stores it with the rest of state."""
        return {
            "node_name": self.node_name,
            "tool_name": self.tool_name,
            "decision": self.decision,
            "input_keys": list(self.input_keys),
            "output_keys": list(self.output_keys),
            "counts": dict(self.counts),
            "latency_ms": self.latency_ms,
            "is_refusal": self.is_refusal,
            "error_type": self.error_type,
        }


#: A graph node: it reads the state and the runtime's context, and returns a state update.
Node = Callable[[Mapping[str, Any], Runtime], Awaitable[dict[str, Any]]]


def recorded(
    node_name: str,
    *,
    reads: Sequence[str] = (),
    counts: Sequence[tuple[str, str]] = (),
    decision_key: str | None = None,
    tool_key: str | None = None,
) -> Callable[[Node], Node]:
    """Wrap a node so that every execution leaves a record. See the module docstring.

    `reads` names the state fields the node uses; `counts` is a list of
    `(label, state key)` pairs, where the key may address a nested mapping as
    `"answer.delta_count"`; `decision_key` names the state key whose value *is* the node's
    decision; `tool_key` names the state key holding the registry name of the tool the
    node called. A declared count whose key the node did not produce is skipped rather
    than raising: the nodes return different shapes on different branches, and a record is
    observability, not a contract the graph must satisfy to run.
    """

    def decorate(node: Node) -> Node:
        @wraps(node)
        async def wrapper(state: Mapping[str, Any], runtime: Runtime) -> dict[str, Any]:
            started = time.perf_counter()
            try:
                result = await node(state, runtime)
            except GraphBubbleUp:
                # A pause, not a failure: `interrupt()` unwinds the node on purpose and the
                # checkpointer keeps the run. See the module docstring.
                raise
            except Exception as error:
                logger.warning(
                    "agent_node_failed",
                    node_name=node_name,
                    error_type=type(error).__name__,
                    latency_ms=_elapsed_ms(started),
                    # Deliberately not `str(error)`: see the module docstring. The
                    # traceback, with its message, is logged by whoever invoked the graph.
                )
                raise
            record = NodeRecord(
                node_name=node_name,
                tool_name=_constant(result, tool_key),
                decision=_constant(result, decision_key),
                input_keys=tuple(reads),
                output_keys=tuple(sorted(key for key in result if key != RECORD_KEY)),
                counts=_counts(result, state, counts),
                latency_ms=_elapsed_ms(started),
                is_refusal=bool(result.get("is_refusal")),
            )
            updates = {key: value for key, value in result.items() if key != RECORD_KEY}
            updates[RECORD_KEY] = [*state.get(RECORD_KEY, ()), record.as_dict()]
            return updates

        return wrapper

    return decorate


def records_of(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The records a run has accumulated, as a list. A reader's accessor, not a node's."""
    return list(state.get(RECORD_KEY, ()))


def node_names(state: Mapping[str, Any]) -> list[str]:
    """The names of the nodes that have run, in order — how a test shows what *did* run."""
    return [str(record.get("node_name")) for record in records_of(state)]


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _constant(result: Mapping[str, Any], key: str | None) -> str | None:
    """A declared constant the node produced — its decision, or the tool it called.

    `str()` of an enum member is not its value — hence `getattr(..., 'value', ...)`, so an
    `Intent` records `"forbidden"` rather than `"Intent.FORBIDDEN"`. The key may address a
    nested mapping, as `counts` may. Both keys this serves are constants by contract: an
    enum member the classifier chose, or a key of the tool registry.
    """
    if key is None:
        return None
    value = _lookup(result, key)
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _counts(
    result: Mapping[str, Any],
    state: Mapping[str, Any],
    declared: Sequence[tuple[str, str]],
) -> dict[str, int]:
    """The declared counts, reduced to integers. See `recorded` for the key syntax."""
    counted: dict[str, int] = {}
    for label, key in declared:
        value = _lookup(result, key)
        if value is None:
            value = _lookup(state, key)
        size = _size(value)
        if size is not None:
            counted[label] = size
    return counted


def _lookup(source: Mapping[str, Any], key: str) -> Any:
    """One level of `a.b` navigation, because a node's counts live in its own sub-dict."""
    head, _, tail = key.partition(".")
    value = source.get(head)
    if not tail:
        return value
    if isinstance(value, Mapping):
        return value.get(tail)
    return None


def _size(value: Any) -> int | None:
    """A length for a string or a collection, the number itself for an int, nothing else.

    `bool` is excluded before `int`: `True` is not a count of one, it is a flag that already
    has its own field (`is_refusal`).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (str, bytes, list, tuple, set, frozenset, dict)):
        return len(value)
    return None


__all__ = [
    "ALLOWED_FIELDS",
    "RECORD_KEY",
    "NodeRecord",
    "node_names",
    "recorded",
    "records_of",
]
