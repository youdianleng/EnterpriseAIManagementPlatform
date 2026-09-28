"""Everything the skeleton says without a model: refusals, placeholders, small talk.

**Why the refusals live here and not behind the model.** D23 and DESIGN §6.2 put four asks
beyond the system's competence — another person's salary or attendance, performance or
promotion advice, and any request to change the database — and the checklist requires them
refused 「在代码层」 and 「不转发给模型去"委婉处理"」. A refusal that a model writes is a
refusal a cleverly worded question can talk its way around; a refusal that is a *constant*
is not. So the copy below is a table, keyed by the rule name that `intents.py` decided, and
the graph's `refuse` node is a branch that constructs no model call at all.

**Bilingual, like ticket 34's refusal, and for the same reason.** §10.4 leaves the interface
language to the browser, and a refusal may be read by somebody who is not the asker (the
same reason `domain/answer/prompts.py` stores both languages in the message body). Each
refusal here carries:

* `message_key` — the key a client renders in the reader's own language. **The wording
  lives in `app/core/messages.py` and is read from there**, not repeated here: that module
  is the one place this project keeps user-facing sentences, and a refusal written twice is
  a refusal that will disagree with itself after the first edit. `test_agent_graph.py`
  asserts every key below is in both catalogues, so a key added here without wording fails
  a test rather than a request.
* `es` and `en`, and `text` — the two sentences and the block they form, so the graph can
  return the whole refusal the way §5.2's `refusal` event does, with no second lookup.

**The placeholders are not copy, they are scaffolding, and they say which ticket fills
them.** Tickets 39-41 add the read-only tools, the draft tools and the human confirmation;
until then a branch that would call a tool says so in a sentence that names the ticket, and
a test asserts that sentence rather than leaving a reader to infer the branch is unfinished.
"""

from dataclasses import dataclass

from app.core.messages import MESSAGES

#: The four keys, one per rule of `intents.FORBIDDEN_RULES`, in the `errors.` namespace the
#: whole catalogue uses — including ticket 34's `errors.knowledge_base_no_basis`, which is
#: not an error either.
SALARY_OF_ANOTHER_KEY = "errors.forbidden_salary_of_another"
ATTENDANCE_OF_ANOTHER_KEY = "errors.forbidden_attendance_of_another"
PERFORMANCE_OR_PROMOTION_ADVICE_KEY = "errors.forbidden_performance_or_promotion_advice"
DATABASE_WRITE_KEY = "errors.forbidden_database_write"


@dataclass(frozen=True, slots=True)
class Refusal:
    """One refusal, in both languages, with the key a client renders it by.

    `rule` is the classifier's own rule name, kept so that a reader of a streamed refusal
    can tell *which* prohibition was hit without re-reading the question. It is a constant
    from `intents.py`, never text from the request.
    """

    rule: str
    message_key: str
    es: str
    en: str

    @property
    def text(self) -> str:
        """Both languages, as one block. Ticket 34's `refusal_text()` shape."""
        return f"{self.es}\n\n{self.en}"

    def as_dict(self) -> dict[str, str]:
        """The refusal as state carries it: names and copy, no conversation text."""
        return {
            "rule": self.rule,
            "message_key": self.message_key,
            "es": self.es,
            "en": self.en,
            "text": self.text,
        }


def _refusal(rule: str, message_key: str) -> Refusal:
    """One refusal, with its wording read from the catalogue. See the module docstring.

    The lookup is at import time and unguarded on purpose: a key with no wording is a
    mistake that should stop the process that imports this module, not one that should be
    discovered by an employee reading a blank refusal.
    """
    return Refusal(
        rule=rule,
        message_key=message_key,
        es=MESSAGES["es"][message_key],
        en=MESSAGES["en"][message_key],
    )


#: The four refusals, keyed by the rule name `intents.FORBIDDEN_RULES` gives them. Each copy
#: says what the system will not do **and what it will do instead** — a refusal that only
#: says "no" leaves a person stuck, and every one of these four has a legitimate route
#: (their own payslip, their own attendance, HR, or the screen that owns the write).
REFUSALS: dict[str, Refusal] = {
    "salary_of_another": _refusal("salary_of_another", SALARY_OF_ANOTHER_KEY),
    "attendance_of_another": _refusal("attendance_of_another", ATTENDANCE_OF_ANOTHER_KEY),
    "performance_or_promotion_advice": _refusal(
        "performance_or_promotion_advice", PERFORMANCE_OR_PROMOTION_ADVICE_KEY
    ),
    "database_write": _refusal("database_write", DATABASE_WRITE_KEY),
}


class NotAForbiddenRule(LookupError):
    """Raised when a rule that is not one of D23's four reaches the refusal path.

    It can only be a wiring bug — the `refuse` node is reachable exactly when the
    classifier decided `Intent.FORBIDDEN` — and it is raised rather than answered with a
    generic sentence because a refusal that names the wrong prohibition is worse than an
    error an operator can see.
    """


def refusal_for(rule: str) -> Refusal:
    """The refusal for a rule name, or `NotAForbiddenRule`.

    The guard is the point: without it, a routing mistake would produce a KeyError from a
    dict lookup, and with a `.get(rule, default)` it would produce a *plausible* refusal for
    a question that was never prohibited. Neither is acceptable in the branch whose whole
    job is to be unambiguous.
    """
    try:
        return REFUSALS[rule]
    except KeyError as error:
        raise NotAForbiddenRule(
            f"{rule!r} is not one of the four prohibited asks "
            f"({', '.join(sorted(REFUSALS))}); the refusal path was reached with a rule "
            "that is not a prohibition, which is a routing bug"
        ) from error


#: 闲聊. A fixed reply rather than a model call, deliberately: two of DESIGN §6.1's four
#: branches lead to a model and this one leads nowhere, so a greeting costs nothing and
#: cannot hallucinate a policy. It also states what the assistant *can* do, which is the
#: only useful thing a greeting from this system can say.
SMALL_TALK_REPLY = (
    "¡Hola! Puedo responder preguntas sobre las políticas de la empresa (con sus fuentes), "
    "consultar tus propios datos de jornada y vacaciones, o preparar una solicitud para que "
    "la confirmes tú.\n\n"
    "Hello! I can answer questions about company policy (with sources), look up your own "
    "attendance and leave data, or draft a request for you to confirm."
)

#: 只读数据查询's branch, until ticket 39 registers the tools of §6.2's read-only half.
NO_READ_ONLY_TOOL = (
    "The graph routed this to the read-only tool branch, but no read-only tool is "
    "registered yet: ticket 39 adds get_my_attendance, get_my_leave_balance, "
    "get_my_timesheets, get_colleague_contact, get_team_attendance_summary and "
    "search_policy. Nothing was queried."
)

#: 待办操作's branch, until ticket 40 registers the draft tools.
NO_DRAFT_TOOL = (
    "The graph routed this to the draft tool branch, but no draft tool is registered yet: "
    "ticket 40 adds draft_leave_request, draft_attendance_correction and draft_timesheet, "
    "each producing a PrefillForm rather than writing anything. Nothing was drafted and "
    "nothing was written."
)

#: What the interruption is waiting for. **This is a placeholder payload, and it says so.**
#: DESIGN §6.3 requires the real one to be a complete, editable PrefillForm whose
#: confirmation is an explicit button click; ticket 40 produces the form and ticket 41
#: implements the confirmation, the re-validation and the `agent_actions` trail. What this
#: ticket delivers is the *mechanism* — `interrupt()` on a Postgres-checkpointed thread —
#: and a payload that names what is missing.
CONFIRMATION_PENDING = {
    "awaiting": "human_confirmation",
    "draft": None,
    "notice": NO_DRAFT_TOOL,
    "filled_by": "ticket 40 (draft tools and PrefillForm)",
    "handled_by": "ticket 41 (confirmation, re-validation, agent_actions)",
}


__all__ = [
    "ATTENDANCE_OF_ANOTHER_KEY",
    "CONFIRMATION_PENDING",
    "DATABASE_WRITE_KEY",
    "NO_DRAFT_TOOL",
    "NO_READ_ONLY_TOOL",
    "PERFORMANCE_OR_PROMOTION_ADVICE_KEY",
    "REFUSALS",
    "SALARY_OF_ANOTHER_KEY",
    "SMALL_TALK_REPLY",
    "NotAForbiddenRule",
    "Refusal",
    "refusal_for",
]
