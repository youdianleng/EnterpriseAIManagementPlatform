"""The values a tool is made of: its name, its kind, its arguments, and its result.

DESIGN §6.2 is a whitelist of nine tools and this module holds the four shapes every
entry of it shares. Four decisions are worth reading before the code:

**`ToolKind` has no `WRITE`, and that is the constraint's first layer.** D22 and
`codebase-design.md` §6's constraint B say the agent never writes the database. The
first of the three defences is structural: `ai/**` reaches no write repository and the
tool set contains no write tool. A tool that wrote could not be described by this type
at all, which is worth more than a rule saying not to write one.

**`Tool.parameters` is a closed vocabulary, checked when a tool is built.** The
checklist's non-negotiable half is 「所有只读工具以**被调用者的身份**执行查询」, and the
sharpest way to say that in types is: no tool may declare a parameter that names an
employee. `ALLOWED_PARAMETERS` is that vocabulary, `Tool.__post_init__` refuses
anything outside it, and `tests/test_agent_readonly_tools.py` reads the registry and
asserts the union of every tool's parameters is inside it. A parameter called
`employee_id` would not merely be unused — it could not be constructed.

**`ToolContext` is what one execution is given: the caller, a session, and today.**
`principal` is the whole of "as whom", and there is deliberately no field that could
name a second person. `today` is passed rather than read from the clock, because a
tool whose default period is "this month" must be testable on a fixed day.

**`ToolResult` carries structured values and never a sentence.** The checklist's
「工具返回结构化数据，回答中的数字直接来自查询结果」 is enforced by where the copy
lives: a tool returns `data` (numbers, dates, statuses) and `render.py` composes the
answer *from* that data, so there is no code path in which a tool states a figure it
did not read. `outcome` is what tells a caller whether `data` means anything —
`REFUSED` is the permission kernel's answer, `FAILED` is a query that raised, and
neither carries a figure at all.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access.principal import Principal


class ToolKind(StrEnum):
    """The two kinds of tool §6.2 allows. There is deliberately no `WRITE`."""

    #: §6.2's read-only half: `get_my_attendance`, `get_my_leave_balance`,
    #: `get_my_timesheets`, `get_colleague_contact`, `get_team_attendance_summary`.
    #: (`search_policy` is §6.2's sixth read-only row and is *not* a tool here — see
    #: `registry.py` for why the graph's own RAG branch is the only retrieval path.)
    READ_ONLY = "read_only"
    #: §6.2's draft half: `draft_leave_request`, `draft_attendance_correction`,
    #: `draft_timesheet`. Each produces a `PrefillForm`; none of them writes. Ticket 40.
    DRAFT = "draft"


#: Every parameter name a tool may declare. **The whole vocabulary**, and the reason
#: is in the module docstring: a period, a year, a status and a name are things a
#: question names; an employee id is a thing a *caller* would have to be trusted with.
#: `name` is the one that looks like an exception and is not one: it is the directory's
#: own lookup key, the directory is readable by everybody (`employee.directory`), and
#: what a caller receives for a match is decided by the projection in
#: `domain/employee/visibility.py` — never by the parameter.
#:
#: **Ticket 40 added the draft half of this list, and it is still a closed vocabulary
#: with no way to name a person.** The eight read-only names are periods, a year, a
#: status and a directory string; the draft names below are the fields of a *document*
#: the caller is about to file — a leave type, two dates, a punch kind and its instant,
#: a week and a day, a project and a task, minutes, a note. A draft tool cannot declare
#: `employee_id` for the same reason a read tool cannot: `Tool.__post_init__` refuses it,
#: and `ai/tools/draft.py` takes the subject from `context.principal` and from nowhere
#: else. The request models the eventual submission accepts *do* carry an `employee_id`
#: (`app/api/v1/leave.py::RequestCreate`); the draft form deliberately does not, and
#: `domain/agent/models.py::IDENTITY_FIELDS` names the difference so a test can compare
#: the two shapes and see exactly which fields the platform, not the model, supplies.
ALLOWED_PARAMETERS: Final[frozenset[str]] = frozenset(
    {
        # read-only (ticket 39)
        "from_date",
        "to_date",
        "year",
        "status",
        "name",
        # drafts (ticket 40): the fields of the three documents §6.2 lets the assistant fill in
        "leave_type",
        "start_date",
        "end_date",
        "attachment_reference",
        "business_date",
        "kind",
        "corrected_at",
        "reason",
        "week_start",
        "entry_date",
        "project_id",
        "task_id",
        "minutes",
        "note",
    }
)


class ToolOutcome(StrEnum):
    """How one execution ended. Five values, and only one of them has figures."""

    #: The query ran and its values are in `data` — possibly an empty set of rows,
    #: which is a result and not a failure.
    OK = "ok"
    #: The permission kernel refused the caller. `data` is empty; the answer says so.
    REFUSED = "refused"
    #: **The tool refused what it was asked to produce** (ticket 40). A draft whose
    #: fields would be refused when the employee filed it is not a draft — the checklist
    #: says 「不合法时明确告知原因而不是生成一张注定失败的草稿」 — so a draft tool answers with
    #: the *reason* instead: `data` carries the catalogued `message_key` of the refusal the
    #: submission itself would have raised (`app/core/errors.definition_of`), which is what
    #: makes the sentence the employee reads a sentence the system already owns, in both
    #: languages and with no copy of its own. It is a separate outcome from `REFUSED`
    #: because that one is the permission kernel's answer ("you may not") and this one is
    #: about the *contents* ("these dates cannot be filed") — a client offers a different
    #: remedy for each, and a run's record should not confuse them.
    INVALID = "invalid"
    #: The query raised. `data` is empty and the answer states that the figure could
    #: not be fetched — **never** a plausible number.
    FAILED = "failed"
    #: No tool by that name, or no tool the question can be mapped to. Nothing ran at
    #: all, which is the whitelist's answer (checklist: 「未注册的工具无法被模型调用」).
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What one tool execution is given. See the module docstring.

    Frozen, like `AgentContext`, and for the same reason: the principal a tool acts on
    is the principal the caller resolved, for the whole run, and a tool that could
    swap it mid-run would be a tool that could read as somebody else.
    """

    #: The caller — the only thing that says *whose* data this is.
    principal: Principal
    #: The request's session. A tool builds the domain services it needs on this
    #: session and calls them; it never opens a transaction of its own.
    session: AsyncSession
    #: The Madrid business date. Passed rather than read, so a default period is a
    #: value a test can pin instead of a fact about the day the suite ran.
    today: date


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One invocation: which tool, and with what.

    `arguments` is what a model's function call gives (ticket 42) and what a test
    gives a tool directly. Nothing here is a state field, so a call cannot be
    constructed from the checkpoint: it is built per execution, by the node.
    """

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What one execution produced: an outcome, and — only when it succeeded — values.

    `error_type` is the exception's *class name* and never its message, for the reason
    `agents/records.py` gives about node failures: an exception raised inside a query
    can quote a parameter, and the class name is the part that is safe to keep.
    """

    tool: str
    outcome: ToolOutcome
    data: Mapping[str, Any] = field(default_factory=dict)
    error_type: str | None = None

    @property
    def answered(self) -> bool:
        """Whether the tool read anything at all. The empty set still counts."""
        return self.outcome is ToolOutcome.OK


#: An implementation: it is handed the call and the context and returns a result.
#: A tool that cannot proceed raises rather than returning a partial result —
#: `registry.invoke` is what turns a raise into a stated `FAILED`.
ToolRunner = Callable[[ToolCall, ToolContext], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class Tool:
    """One entry of the whitelist.

    `parameters` is the tool's own argument vocabulary and is validated here rather
    than trusted: see `ALLOWED_PARAMETERS`. `run` is the implementation, and the
    registry is the only place that maps a name to one — `registry.lookup` is the
    single door, which is what makes "a tool the model names that is not registered
    must not run" a property of the code rather than of the prompt.
    """

    name: str
    kind: ToolKind
    summary: str
    parameters: tuple[str, ...]
    run: ToolRunner

    def __post_init__(self) -> None:
        unknown = set(self.parameters) - ALLOWED_PARAMETERS
        if unknown:
            raise ValueError(
                f"tool {self.name!r} declares {sorted(unknown)}, which is not in "
                f"ALLOWED_PARAMETERS {sorted(ALLOWED_PARAMETERS)}; a parameter that "
                "could name an employee is not a tool argument"
            )



class UnknownTool(LookupError):
    """A name that is not in the registry. Raised by `registry.lookup`.

    Loud rather than defaulted, and that is the checklist's 「未注册的工具无法被模型调用」:
    a lookup that fell back to "the first tool" would answer a question nobody asked
    with somebody else's data.
    """


__all__ = [
    "ALLOWED_PARAMETERS",
    "Tool",
    "ToolCall",
    "ToolContext",
    "ToolKind",
    "ToolOutcome",
    "ToolResult",
    "ToolRunner",
    "UnknownTool",
]
