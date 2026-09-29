"""DESIGN §6.2's five read-only tools: the caller's own data, and a manager's reports.

| tool | reach |
|---|---|
| `get_my_attendance(range)` | the caller's own days |
| `get_my_leave_balance(year)` | the caller's own balances |
| `get_my_timesheets(status)` | the caller's own weeks |
| `get_colleague_contact(name)` | 姓名/职位/邮箱/照片 — and only what the projection grants |
| `get_team_attendance_summary(range)` | the attendance of the caller's **direct reports** |

Five decisions are worth reading, and every one of them is a checklist line:

**Every tool runs as the caller, and there is no parameter that could change that.**
Each implementation takes the identity from `context.principal` — never from
`call.arguments` — and `models.ALLOWED_PARAMETERS` makes an employee-naming parameter
unconstructible, so `get_my_attendance` cannot be asked for somebody else's days even
by a caller who names one. Each tool also **asks the permission kernel** for its own
action before reading (`domain/access/kernel.can`), so 「复用权限内核」 is a call and
not a claim: an ordinary employee asking for the team summary is refused by the
kernel's role check, not by a test in this file.

**The manager's reach is the reporting relationship and nothing else.**
`Principal.reports_employee_ids` is the set the kernel's `MANAGER_OF_SUBJECT` clause
tests and the set the approval route is resolved from —
`domain/access/snapshot.py` documents what it does and does not contain (a previous
bug had it include the caller's own approver) — so this module consumes it rather
than re-deriving it from assignments, and asks the kernel again per subject. A
colleague in the same department who does not report to the caller is never in the
candidate set, so their row is never selected: the summary is *empty*, and the answer
for an empty team and for a team with no record in the period is the same sentence.
That is what makes a non-report indistinguishable from nothing to show.

**Contacts are the directory projection, plus §6.2's narrower field list.**
Ticket 07's rule lives in `domain/employee/visibility.py` and this tool calls
`project_directory_row` with the caller's own `ViewerContext` (through
`domain/access/snapshot.to_viewer_context`). Nothing here decides *visibility*: a
field the projection dropped cannot be returned, because the row it dropped it from
is the only row there is. What this file does decide is which of the fields the
projection granted the tool may state — `CONTACT_FIELDS`, which is DESIGN §6.2's
「仅姓名/职位/邮箱/照片」 — and it is a *subset of the projection's own keys*, computed
as one, never a second visibility rule. 住址 and 员工编号 are not keys of the
projection's directory row at all, so they cannot be returned by a mistake here
either.

**Figures are read, never computed into existence.** Each `data` mapping holds the
values the domain service returned — minutes, days, dates, statuses — and the few
aggregates that are computed here (a period's total minutes, a count of days) are
summed over rows the service produced, which is what the API's own readers do.
Nothing here rounds, converts or defaults a figure.

**A failure is a `FAILED` result, not an exception crossing the graph.**
`registry.invoke` is what converts a raise into that outcome, so the shapes below are
straight-line reads: if a service raises, the tool has produced nothing and the answer
is 「无法获取该数据」 with no figure in it.
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any
from uuid import UUID

from app.ai.tools.models import (
    Tool,
    ToolCall,
    ToolContext,
    ToolKind,
    ToolOutcome,
    ToolResult,
)
from app.ai.tools.services import attendance, contact_rows, leave, timesheets
from app.domain.access.kernel import Action, Resource, ResourceKind, can
from app.domain.access.snapshot import to_viewer_context
from app.domain.attendance.models import DayRecord
from app.domain.employee.models import DirectoryEntry
from app.domain.employee.visibility import project_directory_row
from app.domain.timesheet.models import Timesheet

#: How many weeks `get_my_timesheets` reads. A year is 52, and the page states its own
#: total beside what it returned, so the answer can say both without inventing a
#: figure for the weeks it did not read.
WEEKS_READ = 200

#: How many directory matches travel in a result. The answer states the first and the
#: count, and the count is over every match rather than over this slice.
MATCHES_CARRIED = 10

#: DESIGN §6.2's 「仅姓名/职位/邮箱/照片」, as the *keys of the projection's own row* this
#: tool may state. Every name here is a key `project_directory_row` produces; a name it
#: does not produce matches nothing and is silently absent, which is why this can be a
#: filter over the projected row rather than a decision about visibility.
CONTACT_FIELDS = (
    "full_name",
    "preferred_name",
    "job_title_es",
    "job_title_en",
    "email",
    "photo_path",
)

_STATUSES = ("draft", "pending", "approved", "rejected")


def _refused(call: ToolCall) -> ToolResult:
    """The permission kernel said no. `data` is empty, and deliberately so."""
    return ToolResult(tool=call.name, outcome=ToolOutcome.REFUSED)


def _ok(call: ToolCall, data: Mapping[str, Any]) -> ToolResult:
    return ToolResult(tool=call.name, outcome=ToolOutcome.OK, data=data)


def _allows(context: ToolContext, action: Action, subject: UUID | None = None) -> bool:
    """Whether the kernel permits `action` for the caller.

    The resource is an `EMPLOYEE` owned by `subject`, which is the caller themselves
    unless a manager's reach is being decided; `SELF_ONLY_ACTIONS` (your own
    attendance, leave and timesheets) are then allowed only for the owner, and the
    cross-record actions are allowed only for a report. The kernel is asked both times
    rather than role-tested here: `attendance.read_report` is a manager's action, and
    which role holds it is the catalogue's answer, not this module's.
    """
    resource = (
        None
        if subject is None
        else Resource(ResourceKind.EMPLOYEE, owner_employee_id=subject)
    )
    return can(context.principal, action, resource).allowed


# --- the caller's own data ----------------------------------------------------


async def my_attendance(call: ToolCall, context: ToolContext) -> ToolResult:
    """考勤: the caller's own days over a range, with the figures they add up to."""
    if not _allows(context, Action.ATTENDANCE_READ_OWN, context.principal.employee_id):
        return _refused(call)

    from_date, to_date = _period(call)
    days = await attendance(context.session).range_view(
        context.principal.employee_id, from_date, to_date
    )
    return _ok(call, _days(from_date, to_date, days))


async def my_leave_balance(call: ToolCall, context: ToolContext) -> ToolResult:
    """年假: the caller's own balances for one year, including the projected one.

    The balances travel whole — every leave type the service returned — and the
    *answer* states the allowance-backed ones. Which those are is
    `leave_type.counts_against_annual`, a field of the row the service read, so the
    sentence and the ledger cannot disagree about what 年假 is.
    """
    if not _allows(context, Action.LEAVE_READ_OWN, context.principal.employee_id):
        return _refused(call)

    year = int(_argument(call, "year"))
    views = await leave(context.session).balances(
        context.principal.employee_id, year=year
    )
    balances = [_balance(view, year) for view in views]
    return _ok(
        call,
        {
            "year": year,
            "balances": balances,
            "annual": [item for item in balances if item["counts_against_annual"]],
        },
    )


async def my_timesheets(call: ToolCall, context: ToolContext) -> ToolResult:
    """工时表状态: the caller's own weeks, counted by status.

    `status` narrows the weeks *carried in the result* rather than the counts: the
    counts are over everything the page returned, so the answer states the caller's
    real position and the filter is visible in `data` as `filtered_by`. A filtered
    count would answer "how many of my weeks are pending" with "how many of the
    pending ones are pending", which is not a question anybody asks.
    """
    if not _allows(context, Action.TIMESHEET_READ_OWN, context.principal.employee_id):
        return _refused(call)

    status = str(call.arguments.get("status") or "")
    page = await timesheets(context.session, context.principal).list_weeks(
        limit=WEEKS_READ
    )
    counts = dict.fromkeys(_STATUSES, 0)
    for sheet in page.items:
        name = str(sheet.status)
        counts[name] = counts.get(name, 0) + 1
    return _ok(
        call,
        {
            "weeks": page.total,
            "listed": len(page.items),
            "filtered_by": status or None,
            "counts": counts,
            **counts,
            "sheets": [
                {"week_start": sheet.week_start.isoformat(), "status": str(sheet.status)}
                for sheet in _carried(page.items, status)
            ],
        },
    )


async def colleague_contact(call: ToolCall, context: ToolContext) -> ToolResult:
    """通讯录: one colleague's name, position, email and photo — never more.

    The row is `project_directory_row`'s, built with the *caller's* viewer context, so
    ticket 07's visibility rule is the one that answers. A name is matched against the
    directory listing locally because the employee module publishes no search:
    `EmployeeRepository.list_directory` is the whole read, and filtering it here adds no
    rule — every match still goes through the same projection.
    """
    if not _allows(context, Action.EMPLOYEE_DIRECTORY, context.principal.employee_id):
        return _refused(call)

    query = str(call.arguments.get("name") or "").strip()
    if not query:
        # No name is not a lookup. `selection.arguments_for` returns `None` for this
        # case, so reaching here means a caller named the tool directly; refusing beats
        # listing the directory, which is the difference between an answer and a dump.
        return ToolResult(
            tool=call.name, outcome=ToolOutcome.FAILED, error_type="MissingArgument"
        )

    viewer = to_viewer_context(context.principal)
    entries = await contact_rows(context.session)
    matches = [entry for entry in entries if _matches_name(entry, query)]
    rows = [
        {
            name: projected[name]
            for name in CONTACT_FIELDS
            if name in projected
        }
        for projected in (
            project_directory_row(viewer, entry) for entry in matches[:MATCHES_CARRIED]
        )
    ]
    first = rows[0] if rows else {}
    return _ok(
        call,
        {
            "query": query,
            "match_count": len(matches),
            "matches": rows,
            # The projected row's own fields, or nothing. `visibility.py` drops the
            # email rather than nulling it so that "withheld" cannot be read as "not
            # recorded"; reading it with `.get` keeps that shape all the way out.
            "full_name": first.get("full_name"),
            "job_title_es": first.get("job_title_es"),
            "job_title_en": first.get("job_title_en"),
            "email": first.get("email"),
            "photo_path": first.get("photo_path"),
        },
    )


async def team_attendance_summary(call: ToolCall, context: ToolContext) -> ToolResult:
    """经理视图: the attendance of the caller's **direct reports**, and nobody else.

    Three facts make the reach exactly 直属下属, and each is a different mechanism:

    * the kernel is asked for `attendance.read_report` — the manager's action — with no
      resource, so a caller who is not a manager is refused before any query runs;
    * the candidate set is `Principal.reports_employee_ids`, the relationship the
      kernel's own `MANAGER_OF_SUBJECT` clause tests, consumed from the snapshot
      instead of re-derived from assignments;
    * every candidate is then put back to the kernel as a resource, so the decided
      reach is the kernel's and the snapshot only proposes.

    A colleague in the same department appears in none of the three, so their days are
    never selected — not filtered out afterwards, never read. With nothing read the
    result is an empty summary, and `render._team_attendance` says one sentence for it
    whichever way the emptiness arose.
    """
    if not _allows(context, Action.ATTENDANCE_READ_REPORT):
        return _refused(call)

    from_date, to_date = _period(call)
    reachable = sorted(
        subject
        for subject in context.principal.reports_employee_ids
        if _allows(context, Action.ATTENDANCE_READ_REPORT, subject)
    )
    if not reachable:
        return _ok(call, _team(from_date, to_date, []))

    days = attendance(context.session)
    names = {entry.employee_id: entry.full_name for entry in await contact_rows(context.session)}
    reports: list[dict[str, Any]] = []
    for subject in reachable:
        summary = _days(
            from_date, to_date, await days.range_view(subject, from_date, to_date)
        )
        # The period's own days are not repeated per person: the answer states a total per
        # report, and a hundred employees' day rows would make the checkpoint carry a
        # payroll-sized table for a sentence that says one figure.
        reports.append(
            {
                "employee_id": str(subject),
                "full_name": names.get(subject),
                "worked_minutes": summary["worked_minutes"],
                "worked_days": summary["worked_days"],
                "punches": summary["punches"],
                "last_out": summary["last_out"],
            }
        )
    return _ok(call, _team(from_date, to_date, reports))


# --- internals ----------------------------------------------------------------


def _days(
    from_date: date, to_date: date, records: Sequence[DayRecord]
) -> dict[str, Any]:
    """A period of one person's days, as the figures a caller asks about.

    `range_view` is complete by construction — every day of the range, gaps included —
    so `punched` is the days anything happened on, and it is what tells "nothing was
    recorded" from "a period of zeros". A total is summed over the rows the service
    derived the per-day figures on; nothing here recomputes a day.
    """
    punched = [record for record in records if _has_punches(record)]
    last_out = max(
        (record.last_out for record in records if record.last_out is not None),
        default=None,
    )
    return {
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "worked_minutes": sum(record.worked_minutes for record in records),
        "worked_days": sum(1 for record in records if record.worked_minutes > 0),
        "punches": len(punched),
        "last_out": last_out.isoformat() if last_out is not None else None,
        "days": [_day(record) for record in punched],
    }


def _team(
    from_date: date, to_date: date, reports: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """The summary's shape.

    `people` counts the reports the summary covered and `punches` counts the days any
    of them worked; with an empty `reports` both are zero, which is the same shape a
    team with nothing recorded produces.
    """
    return {
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "people": len(reports),
        "worked_minutes": sum(int(report["worked_minutes"]) for report in reports),
        "punches": sum(int(report["punches"]) for report in reports),
        "reports": list(reports),
    }


def _period(call: ToolCall) -> tuple[date, date]:
    """The range a call names. A malformed one raises, and `invoke` states the failure."""
    from_date = _as_date(_argument(call, "from_date"))
    to_date = _as_date(call.arguments.get("to_date") or from_date.isoformat())
    return from_date, to_date


def _argument(call: ToolCall, name: str) -> Any:
    value = call.arguments.get(name)
    if value in (None, ""):
        raise ValueError(f"{call.name} needs a {name}")
    return value


def _as_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value))


def _has_punches(day: DayRecord) -> bool:
    """Whether anything happened on this day at all.

    A day in the range always exists — `range_view` is complete by construction, gaps
    included — so "the day is present" cannot be the test. A punch is.
    """
    return day.first_in is not None or day.last_out is not None


def _day(day: DayRecord) -> dict[str, Any]:
    """One day, as JSON-native values: the state it travels in is checkpointed as JSON."""
    return {
        "business_date": day.business_date.isoformat(),
        "status": str(day.status),
        "first_in": day.first_in.isoformat() if day.first_in is not None else None,
        "last_out": day.last_out.isoformat() if day.last_out is not None else None,
        "worked_minutes": day.worked_minutes,
    }


def _balance(view, year: int) -> dict[str, Any]:  # noqa: ANN001 - a LeaveBalanceView
    """One balance, including the service's own `remaining_days`.

    That property is the module's arithmetic, not this file's: `LeaveBalance` computes
    it so that the figure a refusal states and the figure a request is checked against
    cannot differ, and a tool that re-derived it would be a second place the rule is
    written.
    """
    return {
        "year": year,
        "code": view.leave_type.code,
        "leave_type_es": view.leave_type.name_es,
        "leave_type_en": view.leave_type.name_en,
        "counts_against_annual": view.leave_type.counts_against_annual,
        "projected": view.projected,
        "entitled_days": view.balance.entitled_days,
        "carried_over_days": view.balance.carried_over_days,
        "used_days": view.balance.used_days,
        "pending_days": view.balance.pending_days,
        "remaining_days": view.balance.remaining_days,
    }


def _matches_name(entry: DirectoryEntry, query: str) -> bool:
    """A case-insensitive substring of the name the directory shows.

    `full_name` and `preferred_name` only: matching against an email would let a
    question address somebody the directory does not name, and the tool's subject is
    同事, not an address book.
    """
    needle = query.casefold()
    haystacks = (entry.full_name or "", entry.preferred_name or "")
    return any(needle in haystack.casefold() for haystack in haystacks)


def _carried(sheets: Sequence[Timesheet], status: str) -> Sequence[Timesheet]:
    """The weeks the result carries: all of them, or the ones of one status."""
    if not status:
        return sheets
    return [sheet for sheet in sheets if str(sheet.status) == status]


#: The whitelist's read-only half, keyed by the name a caller uses. A plain dict here
#: and a `MappingProxyType` in `registry.py`, because that is the object a reader of
#: the registry should find: this is the literal, that is the registry.
READ_ONLY_TOOLS: dict[str, Tool] = {
    "get_my_attendance": Tool(
        name="get_my_attendance",
        kind=ToolKind.READ_ONLY,
        summary="The caller's own punches and daily records over a date range",
        parameters=("from_date", "to_date"),
        run=my_attendance,
    ),
    "get_my_leave_balance": Tool(
        name="get_my_leave_balance",
        kind=ToolKind.READ_ONLY,
        summary="The caller's own leave balances for one year",
        parameters=("year",),
        run=my_leave_balance,
    ),
    "get_my_timesheets": Tool(
        name="get_my_timesheets",
        kind=ToolKind.READ_ONLY,
        summary="The caller's own timesheet weeks, by status",
        parameters=("status",),
        run=my_timesheets,
    ),
    "get_colleague_contact": Tool(
        name="get_colleague_contact",
        kind=ToolKind.READ_ONLY,
        summary="A colleague's name, position, email and photo, as the directory projects them",
        parameters=("name",),
        run=colleague_contact,
    ),
    "get_team_attendance_summary": Tool(
        name="get_team_attendance_summary",
        kind=ToolKind.READ_ONLY,
        summary="The attendance of the caller's direct reports",
        parameters=("from_date", "to_date"),
        run=team_attendance_summary,
    ),
}


__all__ = [
    "CONTACT_FIELDS",
    "MATCHES_CARRIED",
    "READ_ONLY_TOOLS",
    "WEEKS_READ",
    "colleague_contact",
    "my_attendance",
    "my_leave_balance",
    "my_timesheets",
    "team_attendance_summary",
]
