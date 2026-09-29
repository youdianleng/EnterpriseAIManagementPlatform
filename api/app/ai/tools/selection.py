"""Which read-only tool a question wants, and with what period.

**This module is the seam ticket 42 replaces, and it says so.** §6.2's registry is a
whitelist of tools; the choice among them is a model's function call in the finished
system (the ticket that lands the provider chain and the observability work), and
until then the choice has to come from somewhere. It comes from here: a small lexical
selector in the same style as `agents/intents.py`, with the same documented weakness
— it reads words, not meaning.

**Two layers, and the difference between them is why this is not a second
classifier.** `agents.intents.classify` answers 「is this a data query at all?」 and
routes it; this answers 「which data?」 for the branch that already decided it is one.
The two share a vocabulary by necessity — both read the same words — and the
duplication is recorded rather than hidden: when ticket 42 puts a model in this
position, *this* file is deleted and the classifier keeps its job.

**A tool that is named is looked up, never assumed.** `select_tool` returns a name,
and `registry.lookup` is the only door to an implementation: a name this module
produced that no tool carries is refused by the registry exactly as a name a model
invented is. `tests/test_agent_readonly_tools.py` asserts the two agree — every name
below is in the registry — so the selector cannot name a tool that does not exist.

**No selection names a person.** `get_colleague_contact` is the one tool whose
argument is a name, and it is a *search string* for a directory everybody may read;
the four self tools take a period, and the team tool takes a period and reads only
`Principal.reports_employee_ids`. There is no code path here — and no parameter in
`models.ALLOWED_PARAMETERS` — by which a question could ask for somebody else's
record.
"""

import re
from datetime import date, timedelta
from typing import Any, Final

from app.ai.tools.models import ToolCall

#: A period the caller did not name. The month is the unit a person asks about
#: ("this month's hours") and the unit the payroll calendar already works in.
Month = tuple[date, date]


def month_of(day: date) -> Month:
    """The first and last day of `day`'s month, both included."""
    first = day.replace(day=1)
    if first.month == 12:
        after = first.replace(year=first.year + 1, month=1)
    else:
        after = first.replace(month=first.month + 1)
    return first, after - timedelta(days=1)


def _group(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern) for pattern in patterns)


def _matches(groups: tuple[re.Pattern[str], ...], text: str) -> bool:
    """Any alternative in any group. One group here means "one subject vocabulary"."""
    return any(pattern.search(text) is not None for pattern in groups)


# --- the subject vocabularies, per tool ---------------------------------------
# Written the way `intents.py` writes its own, and for the same reason: Chinese has no
# spaces, so `re.search` treats a Spanish word and a Chinese phrase identically.

_TEAM = _group(
    r"\bequipo\b", r"\bmi(?:s)?\s+(?:emplead|subordinad|report)",
    r"\bmy\s+(?:team|reports|staff)\b", r"\bteam\s+(?:hours|attendance|summary)\b",
    r"团队", r"下属", r"我的组", r"我的小组", r"我带的",
)

_CONTACT = _group(
    r"correo", r"\bemail\b", r"\be-mail\b", r"contacto", r"tel[ée]fono",
    r"\bextensi[óo]n\b", r"联系方式", r"邮箱", r"邮件", r"电话",
)

_LEAVE = _group(
    r"vacaci", r"permiso", r"saldo", r"\bd[íi]as\b", r"\bleave\b", r"\bbalance\b",
    r"holiday", r"d[íi]as de asuntos", r"假期", r"年假", r"请假", r"余额", r"休假",
)

_TIMESHEET = _group(
    r"timesheet", r"hoja de horas", r"parte de horas", r"imputaci", r"工时表",
    r"工时单", r"报工",
)

_ATTENDANCE = _group(
    r"asistencia", r"fichaj", r"\bfich[óoaeé]", r"jornada", r"horas", r"\bentrad",
    r"\bentr[ée]", r"\bsalida", r"\bsal[ií]", r"attendance", r"clock", r"考勤", r"打卡",
    # 班 alone rather than 上班/下班: Chinese puts 的 between them, and the ticket's own
    # example is 「昨天我几点下的班」 — which contains neither compound. See `_OWN_DATA` in
    # `agents/intents.py`, which makes the same call for the same reason.
    r"出勤", r"工时", r"班",
)

#: The order the tools are tried in. Team before the caller's own data (a manager's
#: question names both), contact before period tools (an email question is about a
#: person, not a month), and the caller's own record last as the broadest of the four.
SELECTORS: Final[tuple[tuple[str, tuple[re.Pattern[str], ...]], ...]] = (
    ("get_team_attendance_summary", _TEAM),
    ("get_colleague_contact", _CONTACT),
    ("get_my_leave_balance", _LEAVE),
    ("get_my_timesheets", _TIMESHEET),
    ("get_my_attendance", _ATTENDANCE),
)

#: "Yesterday" as a period of its own, because 「昨天我几点下的班」 is the ticket's own
#: example and a month's summary cannot answer it.
_YESTERDAY = _group(r"\bayer\b", r"\byesterday\b", r"昨天", r"昨日")

#: A four-digit year, which is how a caller names one.
_YEAR = re.compile(r"\b(20\d{2})\b")

#: A name after a particle, before an English possessive, or before the Chinese word for
#: a contact detail. Deliberately narrow: a false negative costs the caller a "which
#: person?" answer, and a false *positive* would send a company name to the directory.
#:
#: The capitals are the point, and this is the one place in the two lexical layers that
#: can use them: `agents.intents.classify` matches the lowercased question, so it can only
#: detect the *shape* of a named person. This function reads the question as it was typed,
#: which is what lets it name one.
_NAME_LATIN = re.compile(
    r"(?:de|del|para)\s+([A-ZÁÉÍÓÚÜÑ][\w'’\-áéíóúüñ]+(?:\s+[A-ZÁÉÍÓÚÜÑ][\w'’\-áéíóúüñ]+){0,2})"
)
_NAME_ENGLISH = re.compile(
    r"([A-ZÁÉÍÓÚÜÑ][\w'’\-áéíóúüñ]+(?:\s+[A-ZÁÉÍÓÚÜÑ][\w'’\-áéíóúüñ]+){0,2})'s\s+"
    r"(?:correo|email|e-mail|contacto|tel[ée]fono|photo)"
)
_NAME_CHINESE = re.compile(
    r"([A-Za-zÁÉÍÓÚÜÑáéíóúüñ][\w'’\-áéíóúüñ]*"
    r"(?:\s+[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][\w'’\-áéíóúüñ]*){0,2})\s*"
    r"(?:的联系方式|的邮箱|的邮件|的电话)"
)

#: The statuses `get_my_timesheets` understands, and the words that mean each.
_STATUSES: Final[tuple[tuple[str, tuple[re.Pattern[str], ...]], ...]] = (
    ("approved", _group(r"aprobad", r"approved", r"\black", r"已批准", r"已通过")),
    ("pending", _group(r"pendient", r"enviad", r"pending", r"submitted", r"待审批", r"待审")),
    ("rejected", _group(r"rechazad", r"devuelt", r"rejected", r"已拒绝", r"被拒")),
    ("draft", _group(r"\bborrador", r"\bdraft\b", r"草稿")),
)


def select_tool(question: str, *, today: date) -> ToolCall | None:
    """The tool the question is about, with its default arguments, or `None`.

    `None` is a real answer: the classifier routes some questions to the read-only
    branch that no registered tool can satisfy (the caller's own payslip, for one —
    §6.2 has no such tool). The node turns that into the "no registered tool" answer
    rather than guessing, because a guess would read data nobody asked about.
    """
    text = question.strip().lower()
    for name, cues in SELECTORS:
        if not _matches(cues, text):
            continue
        arguments = arguments_for(name, question, today=today)
        if arguments is None:
            return None
        return ToolCall(name=name, arguments=arguments)
    return None


def arguments_for(tool: str, question: str, *, today: date) -> dict[str, Any] | None:
    """The arguments a tool needs, read from the question the way the classifier reads it.

    `None` means the question names no argument this tool cannot do without — today
    only `get_colleague_contact`, which cannot search for nobody.
    """
    if tool == "get_my_attendance" or tool == "get_team_attendance_summary":
        return _period(question, today=today)
    if tool == "get_my_leave_balance":
        found = _YEAR.search(question)
        return {"year": int(found.group(1)) if found else today.year}
    if tool == "get_my_timesheets":
        return {"status": _status(question)}
    if tool == "get_colleague_contact":
        name = _person(question)
        return {"name": name} if name else None
    # An unknown name is the registry's to refuse; this function only fills arguments,
    # and inventing a shape for a tool it has never heard of would hide that refusal.
    return {}


def _period(question: str, *, today: date) -> dict[str, Any]:
    """The date range a question asks about: a named yesterday, or the current month."""
    if _matches(_YESTERDAY, question.strip().lower()):
        day = today - timedelta(days=1)
        return {"from_date": day.isoformat(), "to_date": day.isoformat()}
    first, last = month_of(today)
    return {"from_date": first.isoformat(), "to_date": last.isoformat()}


def _status(question: str) -> str:
    """The status a question asks about, or the empty string for "all of them"."""
    text = question.strip().lower()
    for status, cues in _STATUSES:
        if _matches(cues, text):
            return status
    return ""


def _person(question: str) -> str:
    """The name a question asks the directory about, or the empty string."""
    for pattern in (_NAME_LATIN, _NAME_ENGLISH, _NAME_CHINESE):
        found = pattern.search(question)
        if found is not None:
            return " ".join(found.group(1).split())
    return ""


__all__ = ["SELECTORS", "arguments_for", "month_of", "select_tool"]
