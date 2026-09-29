"""What the person is asking for: the five outcomes the checklist names.

    制度问答        → POLICY_QUESTION     a question the knowledge base answers
    只读数据查询     → READ_ONLY_QUERY     "my attendance", "my leave balance"
    待办操作        → PENDING_ACTION      something the employee wants done
    硬禁止请求      → FORBIDDEN           one of D23's four prohibited asks
    闲聊            → SMALL_TALK          a greeting, thanks, small talk

**This is a lexical classifier, and it says so.** Every rule below is a group of regular
expressions that must *all* match the question — a subject group and, where the distinction
matters, a second group that separates "asks about" from "asks me to decide". That is
enough for a deterministic skeleton whose tests can name five questions and assert five
decisions, and it is not a claim to be a good intent classifier. Two consequences are worth
stating rather than discovering:

* **It does not know anybody's name.** 「¿Dónde está la nómina de Marta?」 is not refused by
  the rule below, because no lexical rule can enumerate a company's first names. What
  keeps that question harmless is not this module: retrieval is filtered by §4.2 before it
  runs (ticket 35) and no tool in the registry reads another person's payroll (ticket 39
  may not add one — constraint B). A classifier is a *router*, not a control.
* **The model-backed classifier is a later ticket's seam.** DESIGN §5.3's degradation chain
  and the observability work in ticket 42 are where a model earns the right to make this
  decision. Nothing in the refusal path depends on that: `refuse` is a graph branch that no
  model is ever constructed on, whichever component decided the intent.

**The four prohibited categories are D23's, verbatim** — another person's salary, another
person's attendance, performance/promotion/dismissal advice, and any request to write to
the database. §6.2 names a fifth (uploading or deleting documents, Q39); it is deliberately
**not** a rule here, because the checklist enumerates four and a half-implemented fifth is
worse than a recorded gap. `refuse` is where it lands when ticket 41 or 42 adds it.

**Precedence is part of the interface.** The rules are tried in the order of `RULES` and the
first match decides, so a question that is both a prohibited ask and a plausible data query
(「¿cuántos días de vacaciones tiene mi compañero?」) is refused rather than answered.
`FORBIDDEN_RULES` is therefore first, and `RULES` is built from it rather than repeating it.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Intent(StrEnum):
    """The five outcomes. The value is what the graph's state and records carry."""

    POLICY_QUESTION = "policy_question"
    READ_ONLY_QUERY = "read_only_query"
    PENDING_ACTION = "pending_action"
    FORBIDDEN = "forbidden"
    SMALL_TALK = "small_talk"


#: A group of alternatives. A rule matches when **every** group in its `requires` has at
#: least one alternative that matches the question — which is how "the subject" and "the
#: kind of ask" are combined without writing a predicate function per rule.
Group = tuple[re.Pattern[str], ...]


def _group(*patterns: str) -> Group:
    return tuple(re.compile(pattern) for pattern in patterns)


# --- the subject vocabularies -------------------------------------------------
# Written out as alternatives rather than as a token set, because Chinese has no spaces:
# `re.search` treats a Spanish word and a Chinese phrase identically, and a word-boundary
# rule would need a second code path for the three input languages §5.2 names.

_SALARY = _group(
    r"n[óo]min", r"salario", r"sueldo", r"payroll", r"\bsalar", r"\bwage",
    r"\bgan[ae]n?\b", r"\bcobr[ae]n?\b", r"工资", r"薪资", r"薪水", r"薪酬", r"挣多少",
)

_ATTENDANCE_DATA = _group(
    r"asistencia", r"fichaj", r"\bfich[óoae]", r"jornada", r"horario", r"\bentrad",
    r"\bsalida", r"ausencia", r"vacaci", r"permiso", r"d[íi]as", r"attendance",
    r"clock[- ]?in", r"clock[- ]?out", r"timesheet", r"\bshift", r"\bleave\b",
    r"考勤", r"打卡", r"出勤", r"排班", r"假期", r"请假",
)

#: "somebody who is not the asker". `\bsus?\s+(salary word)` is in the group as well as the
#: nouns, because Spanish marks the owner with a possessive rather than with a preposition:
#: 「¿cuánto cobra su nómina?」 and 「la nómina de mi compañero」 are the same ask.
_ANOTHER_PERSON = _group(
    r"\bde\s+(?:mi\s+)?(?:compañer|colega|jef|otr[oa]s?\b|otra persona|él\b|ella\b|ellos\b)",
    r"\bmi(?:s)?\s+(?:compañer|colega|jef)",
    r"\bsus?\s+(?:n[óo]min|sueld|salari|fichaj|asistencia|jornada|horario|vacaci)",
    r"\b(?:his|her|their|another (?:person|employee)|someone else|other people)\b",
    r"\b(?:colleague|coworker|teammate)s?\b",
    r"他(?:的)?", r"她(?:的)?", r"同事", r"别人", r"其他人的", r"下属的",
)

_PERFORMANCE_SUBJECT = _group(
    r"\bascens", r"\basciend", r"\bascender", r"promoci[óo]n", r"\bpromot", r"\bdespid",
    r"\bfire\b", r"\bfiring\b", r"lay ?off", r"dismiss", r"performance review",
    r"evaluaci[óo]n del desempeño", r"\brendimiento", r"绩效", r"晋升", r"升职", r"解雇",
    r"开除", r"裁员",
)

#: The half that separates 「¿cuál es la política de ascensos?」 — a knowledge-base question
#: this system *should* answer — from 「¿debería ascender a Juan?」, which D23 forbids. Both
#: name promotions; only the second asks the system to make the judgement. §6.2's
#: 生成绩效评价/晋升/解雇建议 is advice, and the advice verb is what this group detects.
_ADVICE_ASK = _group(
    r"\bdeber[íi]a", r"\bdebo\b", r"\bshould\b", r"\bwould you\b", r"recomiend",
    r"\brecommend", r"\bopinas?\b", r"aconsej", r"\badvise\b", r"\bqui[ée]n\b", r"\bwho\b",
    r"该不该", r"应该", r"建议", r"谁该", r"帮我(?:写|做|评价)",
)

_WRITE_VERB = _group(
    r"\bupdate\b", r"\bdelete\b", r"\binsert\b", r"\bdrop\b", r"\btruncate\b", r"\balter\b",
    r"\bescrib", r"\bmodific", r"\bcambi", r"\bejecut", r"\brun\b", r"\bexecute\b",
    r"改(?:一下)?(?:数据库|表|库)", r"写(?:入|进)", r"执行", r"跑一下", r"直接改库",
)

_DATABASE_TARGET = _group(
    r"base de datos", r"\bdatabase\b", r"\bsql\b", r"\bdb\b", r"数据库", r"\btabla\b",
    r"\btable\b",
    # A statement that carries its own target: 「UPDATE employees SET salary = 1」 names no
    # database, because in SQL the table *is* the target. Without these the rule would need
    # every table's name enumerated, and would miss the plainest possible version of the
    # request it exists to refuse.
    r"\bupdate\s+\w+\s+set\b", r"\bdelete\s+from\b", r"\binsert\s+into\b",
    r"\bdrop\s+table\b", r"\btruncate\s+table\b",
)

#: First person, or a question about the asker's own record. Read-only data queries are
#: *allowed* — §6.2's tools `get_my_attendance`, `get_my_leave_balance`, `get_my_timesheets`
#: are the point of the branch — so this group is what keeps them out of `POLICY_QUESTION`.
_OWN = _group(
    r"\bmi\b", r"\bmis\b", r"\bme\b", r"\btengo\b", r"\bquedan\b", r"\bmy\b", r"\bmine\b",
    r"我的", r"我有", r"我还", r"查一下我", r"本人",
    # Ticket 39. Two gaps ticket 38's vocabulary left, both of them in the ticket's own
    # examples of what an employee asks:
    #  * Spanish marks "how many hours have I worked" with the auxiliary `he`, not with a
    #    possessive — 「¿cuántas horas he fichado este mes?」 matched nothing at all;
    #  * Chinese marks the subject with a bare 我 rather than with a possessive, and the
    #    ticket's example is 「昨天我几点下的班」. The pattern is 我 + a *time or quantity*
    #    word rather than 我 alone: 「我同事的年假」 also starts with 我, and a rule that
    #    matched it would read a colleague's question as the caller's own record.
    r"\bhe\b", r"我(?:几|昨|今|上|这|本|下)",
)

_OWN_DATA = _group(
    r"asistencia", r"fichaj", r"jornada", r"horas", r"vacaci", r"permiso", r"saldo",
    r"d[íi]as", r"n[óo]min", r"attendance", r"leave", r"balance", r"timesheet",
    r"holiday", r"overtime", r"考勤", r"假期", r"年假", r"工时", r"余额", r"加班", r"工资条",
    # Ticket 39: the words the ticket's own example uses. 班 alone rather than 上班/下班,
    # because Chinese puts 的 between them — 「昨天我几点下的班」 is a clock-out time and
    # contains neither. 班 is safe here: this group only ever combines with `_OWN`, and a
    # question about somebody else's shifts is refused by `_ANOTHER_PERSON` first.
    r"班", r"打卡",
)

#: 通讯录 (ticket 39). A contact detail and the person it belongs to, as two groups, so
#: that 「¿cuál es el correo de Recursos Humanos?」 reaches the directory tool while a
#: *policy* question that merely mentions a channel ("¿a qué correo escribo para pedir
#: vacaciones?") does not: the second group needs a person, and `_ANOTHER_PERSON` is the
#: vocabulary ticket 38 already wrote for "somebody who is not the asker".
_CONTACT_DETAIL = _group(
    r"correo", r"\bemail\b", r"\be-mail\b", r"contacto", r"tel[ée]fono",
    r"\bextensi[óo]n\b", r"联系方式", r"邮箱", r"邮件", r"电话",
)

#: The person a contact question is about, as `_ANOTHER_PERSON` (ticket 38's vocabulary
#: for "somebody who is not the asker") plus three *positions* a name is addressed from:
#: after a Spanish particle, before an English possessive, and before the Chinese word for
#: a contact detail.
#:
#: **Positions rather than capital letters, and that is forced by `classify`.** This module
#: matches against the lowercased question — which is what makes every other rule
#: case-insensitive — so a `[A-Z]`-anchored proper-name pattern could never fire. The
#: selector (`app/ai/tools/selection.py`) reads the *raw* question instead and does use
#: capitals, which is why it can name the person this rule only detects the shape of.
_PERSON: Group = (
    *_ANOTHER_PERSON,
    re.compile(r"\bde(?:l)?\s+[a-záéíóúüñ]{2,}"),
    re.compile(r"'s\s+(?:correo|email|e-mail|contacto|tel[ée]fono)"),
    re.compile(r"的(?:联系方式|邮箱|邮件|电话)"),
)

#: 我的团队 (ticket 39): a manager asking about their own reports. Two groups again —
#: the team, and a *data* word — so 「¿cuál es la política de mi equipo?」 stays a policy
#: question. The data vocabulary is attendance-and-timesheet-shaped and deliberately has
#: no leave words: §6.2 has no team leave tool, and a rule that matched one would route
#: the question to a branch that cannot answer it.
_TEAM = _group(
    r"\bmi(?:s)?\s+equipo", r"\bmi(?:s)?\s+(?:emplead|subordinad|report)",
    r"\bmy\s+(?:team|reports|staff)\b", r"\bteam\s+(?:hours|attendance|summary)\b",
    r"团队", r"下属", r"我的组", r"我的小组", r"我带的",
)

_TEAM_DATA = _group(
    r"horas", r"fichaj", r"jornada", r"asistencia", r"resumen", r"\bsummary\b",
    r"\bhours\b", r"attendance", r"timesheet", r"工时", r"考勤", r"打卡", r"汇总",
)

_WANT = _group(
    r"\bquiero\b", r"\bquisiera\b", r"\bnecesito\b", r"\bsolicit", r"\bpedir\b",
    r"\bme gustar[íi]a", r"\bap[úu]ntame\b", r"\bregistra", r"\bcorrige", r"\bsubmit\b",
    r"\brequest\b", r"帮我", r"我想", r"我要", r"申请", r"提交", r"补卡", r"请假",
)

_ACTION_SUBJECT = _group(
    r"vacaci", r"permiso", r"\bbaja\b", r"ausencia", r"correcci[óo]n", r"fichaj",
    r"horas extra", r"overtime", r"\bleave\b", r"time off", r"timesheet", r"\bvacation",
    r"请假", r"休假", r"加班", r"补卡", r"年假",
)

_POLICY_SUBJECT = _group(
    r"pol[íi]tica", r"normativa", r"reglamento", r"procedimiento", r"\bpolicy\b",
    r"\bprocedure\b", r"\bcompany\b", r"empresa", r"corresponden", r"se permite",
    r"est[áa] permitido", r"how many days", r"\bthe company\b", r"规定", r"制度", r"政策",
    r"允许吗", r"几天", r"多少天", r"有没有规定",
)

_GREETING = _group(
    r"^hola\b", r"\bhola\b", r"buenos d[íi]as", r"buenas\b", r"\bhey\b", r"\bhi\b",
    r"\bhello\b", r"\bthanks\b", r"gracias", r"thank you", r"qu[ée] tal", r"你好", r"谢谢",
    r"在吗", r"早上好", r"嗨",
)


@dataclass(frozen=True, slots=True)
class Rule:
    """One classification rule: a name, the outcome it decides, and what must match.

    `name` is a name and never the text it matched. It travels into the refusal copy and,
    when a reader needs it, into a log line — which is why it is a constant rather than the
    matched group, and why `records.py` can keep conversation text out of every record.
    """

    name: str
    intent: Intent
    requires: tuple[Group, ...]

    def matches(self, question: str) -> bool:
        """Every group must match somewhere in the question. See `Group`."""
        return all(
            any(pattern.search(question) is not None for pattern in group)
            for group in self.requires
        )


#: D23's four prohibited asks, each with the name the refusal copy is keyed by. First in
#: `RULES`, because a prohibited ask that also looks like a data query must be refused.
FORBIDDEN_RULES: Final[tuple[Rule, ...]] = (
    Rule("salary_of_another", Intent.FORBIDDEN, (_SALARY, _ANOTHER_PERSON)),
    Rule("attendance_of_another", Intent.FORBIDDEN, (_ATTENDANCE_DATA, _ANOTHER_PERSON)),
    Rule(
        "performance_or_promotion_advice",
        Intent.FORBIDDEN,
        (_PERFORMANCE_SUBJECT, _ADVICE_ASK),
    ),
    Rule("database_write", Intent.FORBIDDEN, (_WRITE_VERB, _DATABASE_TARGET)),
)

#: Every rule, in the order they are tried. See the module docstring for why this order and
#: not another: prohibited first, then the caller's own data (before "things to do", so
#: 「quiero saber cuántos días me quedan」 is a query rather than a request), then requests,
#: then the broad knowledge-base rule, then a greeting.
#:
#: `team_data_query` and `contact_query` are ticket 39's two additions, both routed to
#: `Intent.READ_ONLY_QUERY` because both are data the caller may read through §6.2's tools
#: — a manager's own reports, and the published directory. They sit after `own_data_query`
#: so that a question about the caller's *own* record is never taken for one about a
#: colleague, and they are narrow (two groups each, see above) so that a policy question
#: mentioning a team or an email address stays a policy question.
RULES: Final[tuple[Rule, ...]] = (
    *FORBIDDEN_RULES,
    Rule("own_data_query", Intent.READ_ONLY_QUERY, (_OWN, _OWN_DATA)),
    Rule("team_data_query", Intent.READ_ONLY_QUERY, (_TEAM, _TEAM_DATA)),
    Rule("contact_query", Intent.READ_ONLY_QUERY, (_CONTACT_DETAIL, _PERSON)),
    Rule("pending_action", Intent.PENDING_ACTION, (_WANT, _ACTION_SUBJECT)),
    Rule("policy_question", Intent.POLICY_QUESTION, (_POLICY_SUBJECT,)),
    Rule("greeting", Intent.SMALL_TALK, (_GREETING,)),
)

#: What a question that matches nothing is taken to be. The knowledge base is the honest
#: default rather than small talk: an unrecognised question is far more likely to be a
#: badly-worded policy question than a greeting, and the answer path's own refusal (D20)
#: handles "the corpus holds no basis for this" without inventing anything.
DEFAULT_INTENT: Final[Intent] = Intent.POLICY_QUESTION
DEFAULT_RULE: Final[str] = "no_rule_matched"


@dataclass(frozen=True, slots=True)
class Classification:
    """The decision, and the name of the rule that made it.

    Both fields are safe to log and to record: one is one of five values, the other is a
    constant from this module. Neither is derived from the question's text.
    """

    intent: Intent
    rule: str


def classify(question: str) -> Classification:
    """Which of the five the question is, by the first rule that matches it."""
    text = question.strip().lower()
    for rule in RULES:
        if rule.matches(text):
            return Classification(intent=rule.intent, rule=rule.name)
    return Classification(intent=DEFAULT_INTENT, rule=DEFAULT_RULE)


__all__ = [
    "DEFAULT_INTENT",
    "DEFAULT_RULE",
    "FORBIDDEN_RULES",
    "RULES",
    "Classification",
    "Intent",
    "Rule",
    "classify",
]
