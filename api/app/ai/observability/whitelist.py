"""What may leave the process, as a list of *allowed* fields rather than of forbidden ones.

DESIGN §10.1 decided option (A): LangSmith Cloud with a PII filter, because the alternative
— a self-hosted LangSmith needing a licence and four more containers, or Langfuse needing two
— is weight this single-machine installation does not have. The decision's own words are the
whole of this module's job:

    该过滤器必须有单元测试，断言禁止字段不出现在序列化结果中……**只发送**：节点流转、工具名、
    耗时、token 计数、错误类型、命中文档**数量**（非内容）。

**Why a whitelist and why that is not a style preference.** A blacklist of the nine fields
§10.1 names is defeated by the next field somebody adds to a record: `tool_result` is on the
list today, `tool_payload` is not, and the leak is invisible because nothing fails. A
whitelist inverts the failure mode — a field nobody listed does not travel, and the cost of a
new operational field is one line here plus the test that pins it. `FORBIDDEN` still exists
below, but as a **tripwire** rather than as the filter: if one of those nine ever reaches this
layer, something upstream is building a trace out of material it never should have touched,
and that is a bug to raise rather than a value to quietly drop.

**Why the filter is a whitelist *over a serialised payload*.** Two independent checks, and
neither is sufficient alone:

* the **projection** (`project_record`) builds a fresh dict from named fields, so a value that
  is not named cannot survive — whether it sat at the top level, inside `counts`, inside an
  `input_keys` list, or inside a record nested under a key this module has never heard of;
* the **post-condition** (`violations_in`) scans the serialised text of the projected payload
  for the forbidden names, because a projection whose per-field check accepted a *bare*
  string (a `str` is iterable, and `list("secret")` is `['s','e','c',…]`) would pass its own
  unit test while the value went out.

A test that walked only top-level keys would pass with a passage inside a nested dict, so the
tests in `tests/test_trace_redaction.py` plant the forbidden material at four depths — a key
of a node record, a value inside `counts`, an entry inside an `input_keys` tuple, and a whole
record smuggled under an undocumented record key — and assert on `json.dumps` of the result,
never on the dict's top level.

**Why the fields are typed rather than merely named.** `node_name` and `tool_name` are keys of
the graph's own vocabularies and of the tool registry; `decision` and `tool_outcome` are enum
values; `input_keys`, `output_keys` and the keys of `counts` are literals in a decorator;
`error_type` is an exception class's name; the numbers are bounded. Every one of those is a
name the *code* chose, so a whitelisted field cannot be a place model output is written to —
which is the reason ticket 39 stored `tool=None` rather than the name a model invented.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

#: The serialised payload's ceiling. A trace that is bigger than this is not a set of node
#: records — something is being exported wholesale — and refusing it is cheaper than
#: reasoning about what it might contain.
MAX_SERIALISED_CHARS: Final[int] = 16_384

#: How many node records one payload may carry. The graph has seven nodes; the ceiling is
#: permissive because a resumed run appends records, and small enough that a payload cannot
#: grow without bound.
MAX_RECORDS: Final[int] = 64

#: The nesting a record's `counts` may have is one level, and `_copy` enforces the *shape*
#: rather than a depth: a projected value is either a scalar, a list of scalars or a flat
#: dict of scalars, because that is what the per-field predicates accept. There is no depth
#: parameter to get wrong.

#: How long a name, key or error type may be. A runaway value in a whitelisted field is a bug
#: worth refusing rather than shipping, and 64 characters is longer than the longest name in
#: this repository.
MAX_NAME_CHARS: Final[int] = 64

#: The numeric ceiling for a counter. A token count or a duration fits in a day's
#: milliseconds; a number beyond it is not a measurement of anything this graph does.
MAX_COUNT: Final[int] = 2_147_483_647

#: **The whitelist.** DESIGN §10.1's list, as a `frozenset` so that membership is a value a
#: test can assert against rather than a chain of `if`s a reader has to trust.
ALLOWED_TRACE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "node_name",
        "tool_name",
        "decision",
        "input_keys",
        "output_keys",
        "counts",
        "latency_ms",
        "token_in",
        "token_out",
        "is_refusal",
        "error_type",
        "provider_used",
        "model_used",
        "retrieval_hit_count",
    }
)

#: **The nine DESIGN §10.1 names as things that must never travel**, plus the two record keys
#: ticket 41 added to the same family. `tool_input` and `tool_output` were on §10.1's list
#: from the start; `tool_result` and `tool_arguments` are the state keys those two travel
#: under in this repository, and a tripwire that watched only the design's spelling would
#: miss the actual names.
#:
#: This is a **tripwire, not the filter**: `project_record` admits only `ALLOWED_TRACE_FIELDS`,
#: so none of these can be projected in the first place. They are checked so that a future
#: reader who sees one of them arrive gets an exception instead of a quietly redacted trace.
FORBIDDEN: Final[frozenset[str]] = frozenset(
    {
        "content",
        "messages",
        "prompt",
        "completion",
        "query",
        "chunk_text",
        "citations",
        "tool_input",
        "tool_output",
        # The repository's own spelling of the same two, in `AgentState`.
        "tool_arguments",
        "tool_result",
    }
)

#: The substrings the post-condition looks for in the serialised payload, quoted so that
#: `tool_outcome` — a *whitelisted* field — is not mistaken for `tool_output`.
#:
#: The list is **narrowed to the names that cannot legitimately appear** rather than copied
#: from `FORBIDDEN` wholesale, and the reason is a measured false positive: `citations` is a
#: §10.1 forbidden name *and* a count label `records.py` legitimately writes
#: (`counts={"citations": 2}`, the number of citations rather than any of them). A scanner
#: that refused that payload would make the filter unusable for the very record it was built
#: to admit, and an unusable guard is one somebody removes. `content`, `messages`, `prompt`,
#: `query`, `chunk_text`, `tool_input`, `tool_output`, `tool_arguments` and `tool_result` are
#: names no whitelisted field or decorator label uses, so they are safe to scan for.
_FORBIDDEN_MARKERS: Final[tuple[str, ...]] = tuple(
    f'"{name}"'
    for name in (
        "chunk_text",
        "content",
        "messages",
        "prompt",
        "query",
        "tool_arguments",
        "tool_input",
        "tool_output",
        "tool_result",
    )
)

#: The closed value vocabularies of the two enum-shaped fields. Both are keys of catalogues
#: this repository owns — `intents.Intent`'s values and `tools.ToolOutcome`'s — and the reason
#: neither is imported here is that the observability layer must not depend on the graph it
#: observes; the tests assert the two sets agree.
DECISION_VALUES: Final[frozenset[str]] = frozenset(
    {"policy_question", "read_only_query", "pending_action", "forbidden", "small_talk"}
)
TOOL_OUTCOME_VALUES: Final[frozenset[str]] = frozenset(
    {"ok", "refused", "invalid", "failed", "unknown"}
)

#: A node's name, a state key, a count's label: identifiers, and nothing else. This is the
#: check that keeps a whitelisted field from becoming a channel — an identifier cannot carry
#: a sentence, and the values that are not identifiers have their own vocabularies below.
_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z][A-Za-z0-9_]{0,63}\Z")

#: A provider's name and a model's name: `openai`, `deepseek`, `anthropic`, `gpt-4o`,
#: `deepseek-chat`, `claude-3-5-sonnet`. Deliberately tighter than "anything an operator
#: typed", because a model name is a label rather than a channel.
_LABEL: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z][A-Za-z0-9._-]{0,63}\Z")

#: An exception class's name, which is what `records.py` puts in `error_type`.
_EXCEPTION_NAME: Final[re.Pattern[str]] = re.compile(r"\A[A-Z][A-Za-z0-9_]{0,63}\Z")


class ForbiddenTraceField(Exception):
    """A field DESIGN §10.1 forbids was offered to this layer. See `FORBIDDEN`.

    Raised rather than dropped: the layer that built a record out of conversation text or a
    tool's result has a bug, and a redaction that silently absorbed it would let the bug
    survive until a payload that does *not* pass through here carried it out.
    """

    def __init__(self, name: str) -> None:
        super().__init__(
            f"{name!r} is one of DESIGN §10.1's forbidden trace fields; a trace built from "
            f"it means the layer above this one is copying conversation or tool material "
            f"into something it intends to export"
        )
        self.name = name


class TraceTooLarge(Exception):
    """The payload exceeded `MAX_SERIALISED_CHARS`, `MAX_RECORDS` or `MAX_DEPTH`."""


@dataclass(frozen=True, slots=True)
class TraceField:
    """One whitelisted field: its name, and the predicate its value has to satisfy.

    A dataclass rather than a bare `frozenset` because "which fields may leave" and "what
    may each of them hold" are the same decision, and a set of names alone would make the
    second one something every reader has to re-derive from the projection's code.
    """

    name: str
    accepts: Callable[[object], bool]


def _is_count(value: object) -> bool:
    """A bounded non-negative integer, and **not** a `bool`.

    `isinstance(True, int)` is true in Python, so a flag would otherwise be accepted as a
    count of one — the same trap `records._size` documents.
    """
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 0 <= value <= MAX_COUNT
    )


def _is_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER.match(value))


def _is_identifier_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= MAX_NAME_CHARS
        and all(_is_identifier(item) for item in value)
    )


def _is_counts(value: object) -> bool:
    return (
        isinstance(value, dict)
        and len(value) <= MAX_NAME_CHARS
        and all(_is_identifier(key) and _is_count(item) for key, item in value.items())
    )


def _is_label(value: object) -> bool:
    return isinstance(value, str) and bool(_LABEL.match(value))


def _is_exception_name(value: object) -> bool:
    return isinstance(value, str) and bool(_EXCEPTION_NAME.match(value))


def _in(values: frozenset[str]) -> Callable[[object], bool]:
    def accepts(value: object) -> bool:
        return isinstance(value, str) and value in values

    return accepts


#: **The whitelist, field by field.** `project_record` reads this and nothing else, so a
#: field that is not a key here does not travel — which is the property a blacklist cannot
#: have.
TRACE_FIELDS: Final[Mapping[str, TraceField]] = {
    name: TraceField(name, accepts)
    for name, accepts in (
        ("node_name", _is_identifier),
        ("tool_name", _is_identifier),
        ("decision", _in(DECISION_VALUES)),
        ("input_keys", _is_identifier_list),
        ("output_keys", _is_identifier_list),
        ("counts", _is_counts),
        ("latency_ms", _is_count),
        ("token_in", _is_count),
        ("token_out", _is_count),
        ("is_refusal", lambda value: isinstance(value, bool)),
        ("error_type", _is_exception_name),
        ("provider_used", _is_label),
        ("model_used", _is_label),
        ("retrieval_hit_count", _is_count),
    )
}

assert frozenset(TRACE_FIELDS) == ALLOWED_TRACE_FIELDS, (
    "ALLOWED_TRACE_FIELDS and TRACE_FIELDS have drifted apart: one is what the docstrings "
    "and DESIGN §10.1 name, the other is what the projection actually reads"
)
assert not (ALLOWED_TRACE_FIELDS & FORBIDDEN), (
    "a field cannot be both allowed and forbidden; §10.1's two lists are disjoint"
)


def admits(name: object) -> bool:
    """Whether `name` is a field this layer may export. The whitelist, as a question."""
    return isinstance(name, str) and name in TRACE_FIELDS


def field_of(name: str) -> TraceField:
    return TRACE_FIELDS[name]


def reject_forbidden(value: object, *, where: str) -> None:
    """Raise if `value` is a mapping carrying one of §10.1's forbidden field names.

    Called on every mapping the projection walks — the state, each record, each value — and
    it is the tripwire rather than the filter: it costs one membership test per key and it
    converts "somebody put a tool result in the state under its own name" from a silent
    redaction into an exception the caller can read.
    """
    if not isinstance(value, Mapping):
        return
    for key in value:
        if isinstance(key, str) and key in FORBIDDEN:
            raise ForbiddenTraceField(f"{where}.{key}")


def project_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """One node record, reduced to the whitelist. **The filter, and the whole of it.**

    A fresh dict built from named fields rather than a copy with keys removed: a value that
    is not named cannot survive, whatever shape it had, and there is no branch here that a
    later field could be added to by accident. `counts` is rebuilt key by key for the same
    reason — a `dict(counts)` would carry a nested mapping straight through.
    """
    reject_forbidden(record, where="record")
    projected: dict[str, Any] = {}
    for name in sorted(TRACE_FIELDS):
        if name not in record:
            continue
        value = record[name]
        if not field_of(name).accepts(value):
            # A whitelisted *name* whose value is the wrong shape does not travel either: a
            # `counts` that is a string is either a bug or a leak wearing a permitted name,
            # and neither belongs in a payload that leaves the installation.
            continue
        projected[name] = _copy(value)
    return projected


def _copy(value: Any) -> Any:
    """A plain, JSON-encodable copy of a projected value. Never a reference to the state."""
    if isinstance(value, dict):
        return {str(key): _copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy(item) for item in value]
    return value


def violations_in(payload: object) -> frozenset[str]:
    """Which forbidden names or values appear in `payload` once it is serialised.

    The post-condition the ticket asks for, as a function rather than as a comment inside
    the exporter: `export_trace` raises when this is non-empty, and the tests call it
    directly on payloads that were built by hand to carry a passage at a nested depth. The
    serialised form is the thing scanned — not the dict's keys — because the failure this
    exists for is a value that survived a projection whose per-field check looked right.
    """
    try:
        serialised = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - a payload that cannot serialise
        return frozenset({"<unserialisable>"})
    found = {marker.strip('"') for marker in _FORBIDDEN_MARKERS if marker in serialised}
    return frozenset(found)


def forbid_oversized(payload: Mapping[str, Any]) -> None:
    """Refuse a payload past the ceilings. See `MAX_SERIALISED_CHARS` and friends."""
    records = payload.get("records")
    if isinstance(records, list) and len(records) > MAX_RECORDS:
        raise TraceTooLarge(f"{len(records)} records exceeds the {MAX_RECORDS} ceiling")
    serialised = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    if len(serialised) > MAX_SERIALISED_CHARS:
        raise TraceTooLarge(
            f"the serialised trace is {len(serialised)} characters, past the "
            f"{MAX_SERIALISED_CHARS} ceiling"
        )


__all__ = [
    "ALLOWED_TRACE_FIELDS",
    "DECISION_VALUES",
    "FORBIDDEN",
    "MAX_NAME_CHARS",
    "MAX_RECORDS",
    "MAX_SERIALISED_CHARS",
    "TOOL_OUTCOME_VALUES",
    "TRACE_FIELDS",
    "ForbiddenTraceField",
    "TraceField",
    "TraceTooLarge",
    "admits",
    "field_of",
    "forbid_oversized",
    "project_record",
    "reject_forbidden",
    "violations_in",
]
