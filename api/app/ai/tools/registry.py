"""The tool registry: DESIGN §6.2's whitelist, which is **empty until tickets 39-40**.

The graph has a branch for read-only tools and a branch for draft tools, and both of them
ask this module what is registered before they claim anything. Today the answer is nothing,
so both branches return a sentence that names the ticket that will fill them
(`agents/replies.py`) — which is the difference between a seam and a hole.

**The registry has no write kind, and that is the constraint's first layer.** D22 and
`docs/architecture/codebase-design.md` §6's constraint B say the agent never writes the
database. Layer one of the three defences is structural: `ai/**` reaches no write
repository, and the tool set contains no write tool. `ToolKind` therefore has exactly two
members — `READ_ONLY` and `DRAFT` — and a tool that wrote to the database could not be
described by this type at all. Making the forbidden thing unrepresentable is worth more than
a rule saying not to do it, which is the whole reason §6 records three layers instead of
one. Whether that assertion belongs in this ticket is settled by §8's table: constraint B's
structural layer is ticket 40's, and this module is written so that layer has something
specific to assert about.

**A tool's inputs and outputs are never recorded.** §10.1 lists `tool_input` and
`tool_output` among the fields that must never leave the installation, so when a tool lands
here, the record of its execution is its *name*, its duration and its outcome
(`agents/records.py`), never its arguments or its result.
"""

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class ToolKind(StrEnum):
    """The two kinds of tool §6.2 allows. There is deliberately no `WRITE`."""

    #: §6.2's read-only half: `get_my_attendance`, `get_my_leave_balance`,
    #: `get_my_timesheets`, `get_colleague_contact`, `get_team_attendance_summary`,
    #: `search_policy`.
    READ_ONLY = "read_only"
    #: §6.2's draft half: `draft_leave_request`, `draft_attendance_correction`,
    #: `draft_timesheet`. Each produces a `PrefillForm`; none of them writes.
    DRAFT = "draft"


@dataclass(frozen=True, slots=True)
class Tool:
    """One entry of the whitelist. `summary` is for a human reading the registry."""

    name: str
    kind: ToolKind
    summary: str


#: The registry, and the emptiness is the ticket's scope boundary rather than an oversight:
#: ticket 39 registers the read-only tools and ticket 40 the draft tools. A `MappingProxyType`
#: rather than a plain dict so that "who can add a tool" has exactly one answer — an edit to
#: this literal, in the ticket that owns it.
_REGISTRY: Final[MappingProxyType[str, Tool]] = MappingProxyType({})


def registered(kind: ToolKind | None = None) -> tuple[Tool, ...]:
    """The registered tools, optionally of one kind, in name order.

    Name order rather than insertion order, because a caller that picks "the first tool"
    should get the same one whatever order the literal above happens to be written in.
    """
    tools = (
        tool for tool in _REGISTRY.values() if kind is None or tool.kind is kind
    )
    return tuple(sorted(tools, key=lambda tool: tool.name))


__all__ = ["Tool", "ToolKind", "registered"]
