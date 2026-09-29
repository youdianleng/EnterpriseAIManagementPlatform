"""The answer a tool's values become: a sentence built **from** the result, never around it.

The checklist has two halves and this module is both of them:

* 「工具返回结构化数据，回答中的数字直接来自查询结果，不允许模型重算或推测」 — every
  sentence below is `template.format(**data)`, where `data` is the mapping the tool
  returned. There is no arithmetic here, no unit conversion, no rounding and no
  default: a placeholder is filled with the value the query produced, and
  `tests/test_agent_readonly_tools.py` compares the figures in the rendered answer
  against a number computed independently of the tool — raw SQL over the same rows.
* 「工具调用失败时给出明确提示并回退为"无法获取该数据"，不编造数值」 — `FAILED` and
  `REFUSED` have no `data` at all, so there is nothing to format; the sentence is a
  constant from the catalogue and the test asserts the answer contains **no digit**.

**Three outcomes, three sentences, and the third is why this is not a formatter for
`data`.** `REFUSED` is the permission kernel's answer, `FAILED` is a query that
raised, and `unknown` — a name the model produced that is not in the registry — is
the whitelist's answer; all three say that no figure is available, and none of them
mentions a row, a count or a period. A refusal that named a period would tell the
reader that the period exists.

**Bilingual, like the refusals of ticket 38 and for the same reason.** §10.4 leaves
the interface language to the browser, so the answer travels as a key plus both
sentences plus the block they form — the shape `agents/replies.py` established — and
a client renders whichever language its reader is using. The language-specific
*values* (a leave type's name, a job title) follow the same rule: `my_leave_balance`
reads `{leave_type_es}` in the Spanish sentence and `{leave_type_en}` in the English
one, so one render fills two placeholders from the tool's one row rather than
choosing a language here.

**A withheld field is absent from the sentence, not marked.** `colleague_contact`
joins the parts the projection granted; when it withheld the email the sentence
simply has no email, which is what `visibility.py` means by dropping the key. An
answer that said "not visible to you" would restore exactly the distinction the
projection exists to remove.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from app.ai.tools.models import ToolOutcome, ToolResult
from app.core.messages import MESSAGES

#: The three constants. Read from the catalogue at import time, unguarded, for the
#: reason `agents/replies.py` reads its refusals unguarded: a key with no wording is a
#: mistake that should stop the process, not one an employee discovers.
UNAVAILABLE_KEY = "agent.tool.unavailable"
UNKNOWN_KEY = "agent.tool.unknown"
NOT_PERMITTED_KEY = "agent.tool.not_permitted"


@dataclass(frozen=True, slots=True)
class ToolAnswer:
    """One answer to a data question: its key, both sentences, and the block they form."""

    message_key: str
    es: str
    en: str

    @property
    def text(self) -> str:
        """Both languages, as one block — `agents/replies.Refusal.text`'s shape."""
        return f"{self.es}\n\n{self.en}"

    def as_dict(self) -> dict[str, str]:
        """The answer as state carries it. Sentences, a key, and no structured values:
        the values are the tool's result and travel beside this, not inside it."""
        return {
            "message_key": self.message_key,
            "es": self.es,
            "en": self.en,
            "text": self.text,
        }


def _answer(message_key: str, values: Mapping[str, Any] | None = None) -> ToolAnswer:
    """One message from the catalogue, with its placeholders filled from the result."""
    return ToolAnswer(
        message_key=message_key,
        es=MESSAGES["es"][message_key].format(**(values or {})),
        en=MESSAGES["en"][message_key].format(**(values or {})),
    )


def render(result: ToolResult) -> ToolAnswer:
    """The sentence a tool's outcome becomes. See the module docstring."""
    if result.outcome is ToolOutcome.REFUSED:
        return _answer(NOT_PERMITTED_KEY)
    if result.outcome is ToolOutcome.FAILED:
        return _answer(UNAVAILABLE_KEY)
    if result.outcome is ToolOutcome.UNKNOWN:
        return _answer(UNKNOWN_KEY)
    # `_RENDERERS[result.tool]` and not `.get(...)`: a registered tool with no renderer
    # is a wiring bug, and a KeyError here names it. `test_every_registered_tool_has_a
    # _renderer` is what makes it unreachable.
    return _RENDERERS[result.tool](result.data)


def _attendance(data: Mapping[str, Any]) -> ToolAnswer:
    """A period of the caller's own days.

    Three sentences because the data has three shapes — no punches, an open shift, and
    a closed period — and each states the same two figures the query produced. Which
    one applies is decided from `punches` and `last_out`, both of which the tool read.
    """
    if not data["punches"]:
        return _answer("agent.tool.my_attendance.empty", data)
    if data["last_out"] is None:
        return _answer("agent.tool.my_attendance.open", data)
    return _answer("agent.tool.my_attendance", data)


def _leave_balance(data: Mapping[str, Any]) -> ToolAnswer:
    """One sentence per leave type charged against the annual allowance.

    Joined rather than reduced to a total: two allowances do not add up, and a sum
    would be a figure the query never produced. `balances` is what the service
    returned; `annual` is the subset that answers 年假, filtered on the type's own
    `counts_against_annual` flag — a field of the row rather than a rule invented here.
    """
    annual = list(data["annual"])
    if not annual:
        return _answer("agent.tool.my_leave_balance.none", data)
    return ToolAnswer(
        message_key="agent.tool.my_leave_balance",
        es=" ".join(
            MESSAGES["es"]["agent.tool.my_leave_balance"].format(**item) for item in annual
        ),
        en=" ".join(
            MESSAGES["en"]["agent.tool.my_leave_balance"].format(**item) for item in annual
        ),
    )


def _timesheets(data: Mapping[str, Any]) -> ToolAnswer:
    """The caller's weeks, by status. Counts over the rows the page returned."""
    if not data["weeks"]:
        return _answer("agent.tool.my_timesheets.empty", data)
    return _answer("agent.tool.my_timesheets", data)


def _contact(data: Mapping[str, Any]) -> ToolAnswer:
    """One match, or the fact that a name matched nobody.

    `details` is assembled from the projected row's own fields — the title and, when
    the projection granted it, the address. Nothing here can add a field the
    projection dropped, because there is no other field to add.
    """
    if not data["match_count"]:
        return _answer("agent.tool.colleague_contact.not_found", data)
    values = dict(data)
    values["details"] = " · ".join(
        part for part in (data.get("job_title_es"), data.get("email")) if part
    )
    if not values["details"]:
        return _answer("agent.tool.colleague_contact.no_details", values)
    return _answer("agent.tool.colleague_contact", values)


def _team_attendance(data: Mapping[str, Any]) -> ToolAnswer:
    """The caller's direct reports over a period.

    **One sentence for "no reports" and for "no rows".** The ticket requires somebody
    who does not report to the caller to be indistinguishable from nothing to show,
    and a tool that never selects their rows leaves exactly two states to describe:
    nothing was read. `people` is the number of reports the summary covered; `punches`
    counts the days any of them worked.
    """
    if not data["punches"]:
        return _answer("agent.tool.team_attendance.empty", data)
    return _answer("agent.tool.team_attendance", data)


#: tool name → the sentence its values make. Total over the registry, and
#: `test_every_registered_tool_has_a_renderer` is what keeps it that way.
_RENDERERS: dict[str, Callable[[Mapping[str, Any]], ToolAnswer]] = {
    "get_my_attendance": _attendance,
    "get_my_leave_balance": _leave_balance,
    "get_my_timesheets": _timesheets,
    "get_colleague_contact": _contact,
    "get_team_attendance_summary": _team_attendance,
}


def rendered_tools() -> tuple[str, ...]:
    """The tool names this module can render, in name order.

    A reader rather than a private name reached from a test: the mapping above is total
    over the registry, and the test that asserts it should be able to say so without
    importing an underscore.
    """
    return tuple(sorted(_RENDERERS))


__all__ = [
    "NOT_PERMITTED_KEY",
    "UNAVAILABLE_KEY",
    "UNKNOWN_KEY",
    "ToolAnswer",
    "render",
    "rendered_tools",
]
