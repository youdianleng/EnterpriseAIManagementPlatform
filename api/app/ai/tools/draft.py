"""DESIGN §6.2's three draft tools: a filled-in form, and **no write anywhere near them**.

| tool | the form it produces | the submission it is a draft of |
|---|---|---|
| `draft_leave_request` | 请假申请 | `POST /api/v1/leave/requests` |
| `draft_attendance_correction` | 补打卡 | `POST /api/v1/attendance/corrections` |
| `draft_timesheet` | 工时表（一条工时） | `POST /api/v1/timesheets/entries?week=…` |

Five decisions, and every one of them is a checklist line.

**The tools validate with the submission's own rules, because they *are* the submission's
rules.** Ticket 40 extracted `LeaveService.check_request`, `CorrectionService.check_draft`
and `TimesheetService.check_entry` out of the three write paths, and these tools call those
methods. What that buys is the whole value of a draft: a form the employee fills in and
confirms cannot be refused afterwards for a reason the assistant never asked about. There is
no balance check here, no week-lock check and no project check — a second copy of any of
them is the copy that goes stale, and the three `check_*` methods carry the argument in
their own docstrings.

**Nothing here writes, and the walk in `tests/test_agent_draft_tools.py` is what keeps it
true.** The tools read (a leave catalogue, a project's tasks, a balance, a week's status, a
punch) and return values; `codebase-design.md` §6's constraint B is the reason, and the
test walks this module's source with `ast` for a write-shaped call or a write-shaped SQL
literal, exactly as ticket 39's walk does for the read-only half. The record of what the
assistant proposed is written by the *platform* (`app/domain/agent/`), from the node, after
this module has returned — never from inside it.

**The drafted entity is the caller's, and no argument can change that.** Every tool takes
the subject from `context.principal.employee_id`, asks the permission kernel for the action
that filing it would need (`leave.request_own`, `attendance.correction_own`,
`timesheet.write_own` — all self-only in `domain/access/permissions.py`), and declares only
document fields in `parameters`. `models.ALLOWED_PARAMETERS` makes an employee-naming
parameter unconstructible, and `domain/agent/models.py::IDENTITY_FIELDS` records the fields
the submission's request model has and the form deliberately does not.

**An argument the tool cannot use is answered, never guessed.** A draft tool is handed what
a caller or (from ticket 42) a model produced, so a missing or unparsable field is an
ordinary event rather than a bug: the tool returns `ToolOutcome.INVALID` with the *field*
named (`agent.draft.needs_details`), and a document the domain refuses returns `INVALID`
with the catalogued `message_key` of the refusal the submission itself would have raised —
「不合法时明确告知原因而不是生成一张注定失败的草稿」, in both languages, without this module
writing a sentence of its own.

**A form's fields are the submission's fields.** `PrefillForm.fields` names the request
body's own field names, minus the identity fields the platform fills from the session;
`PrefillForm.submit_path` says where the confirmed values go, and the timesheet's carries
its week because that module's week is a query parameter. `tests/test_agent_draft_tools.py`
compares each form with its endpoint's request model, so §6.3's 「完整、可编辑的表单」 is a
test rather than a claim.
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta
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
from app.ai.tools.services import corrections, leave, projects, timesheets
from app.core.errors import ErrorCode, definition_of
from app.core.messages import MESSAGES
from app.domain.access.kernel import Action, Resource, ResourceKind, can
from app.domain.agent.models import (
    DraftEntity,
    FieldKind,
    FieldOption,
    PrefillField,
    PrefillForm,
)
from app.domain.attendance.business_day import MADRID
from app.domain.attendance.models import PUNCH_EVENT_TYPES, EventType
from app.domain.errors import DomainError
from app.domain.leave.models import LeaveRequestCheck
from app.domain.project.models import ProjectQuery, RecordTarget
from app.domain.project.service import ProjectService

#: The key the client renders when a form cannot be filled from what the tool was given.
NEEDS_DETAILS_KEY = "agent.draft.needs_details"

#: What the three forms call themselves, and where a confirmed one would be posted. Written
#: as one table so the field names, the paths and the title keys cannot drift apart between
#: the tools and the test that compares them with the endpoints.
SUBMIT_PATH: dict[DraftEntity, str] = {
    DraftEntity.LEAVE_REQUEST: "/api/v1/leave/requests",
    DraftEntity.ATTENDANCE_CORRECTION: "/api/v1/attendance/corrections",
    DraftEntity.TIMESHEET_ENTRY: "/api/v1/timesheets/entries",
}


class BadArgument(ValueError):
    """A draft argument the tool cannot use.

    Raised for a missing field, an unparsable date and a uuid that is not one — three ways
    of saying the same thing to the employee: the form cannot be filled from what the
    assistant was given, and *which* field is the useful part of the answer. `field` is a
    submission field name, so the reply names the same thing the form will label.
    """

    def __init__(self, field: str, detail: str) -> None:
        super().__init__(detail)
        self.field = field


# --- the three tools ----------------------------------------------------------


async def draft_leave_request(call: ToolCall, context: ToolContext) -> ToolResult:
    """请假申请草稿: a filled-in leave request, or the reason it could not be one.

    The catalogue is read for the form's own select (a code is not a form anybody can fill
    in), and the *check* is `LeaveService.check_request` — the window, the working days, the
    overlap, the attachment rule and the balance, in `draft`'s own order. `facts` carries
    what the check answered: how many working days the leave costs and where the days are
    charged, which is what the employee needs to see beside the dates.
    """
    if not _may(context, Action.LEAVE_REQUEST_OWN):
        return _refused(call)
    try:
        code = _text(call, "leave_type")
        start_date = _date(call, "start_date")
        end_date = _date(call, "end_date")
        reference = _optional_text(call, "attachment_reference")
    except BadArgument as bad:
        return _needs_details(call, (bad.field,))

    service = leave(context.session)
    catalogue = await service.list_types()
    try:
        check = await service.check_request(
            employee_id=context.principal.employee_id,
            code=code,
            start_date=start_date,
            end_date=end_date,
            attachment_reference=reference,
        )
    except DomainError as refusal:
        return _invalid(call, refusal)

    form = PrefillForm(
        tool=call.name,
        entity=DraftEntity.LEAVE_REQUEST,
        title_key=_title_key(DraftEntity.LEAVE_REQUEST),
        title_es=_title(DraftEntity.LEAVE_REQUEST, "es"),
        title_en=_title(DraftEntity.LEAVE_REQUEST, "en"),
        submit_path=SUBMIT_PATH[DraftEntity.LEAVE_REQUEST],
        fields=(
            _select(
                "leave_type",
                value=check.leave_type.code,
                options=tuple(
                    FieldOption(
                        value=item.code, label_es=item.name_es, label_en=item.name_en
                    )
                    for item in catalogue
                ),
            ),
            _date_field("start_date", check.start_date),
            _date_field("end_date", check.end_date),
            PrefillField(
                name="attachment_reference",
                label_key="agent.draft.field.attachment_reference",
                kind=FieldKind.TEXT,
                label_es=_label("attachment_reference", "es"),
                label_en=_label("attachment_reference", "en"),
                value=check.attachment_reference,
                # Required exactly when the type demands it, which is the module's own
                # rule (`LeaveService._require_attachment`) and not a second one: a field
                # the submission would refuse empty is marked, and one it accepts is not.
                required=check.leave_type.requires_attachment,
                hint_es=_hint("attachment_reference", "es"),
                hint_en=_hint("attachment_reference", "en"),
            ),
        ),
        facts=_leave_facts(check),
    )
    return _drafted(call, form)


async def draft_attendance_correction(call: ToolCall, context: ToolContext) -> ToolResult:
    """补打卡草稿: a filled-in correction request, or the reason it could not be one.

    The instant is shown as the *time of day* on the business date beside it
    (`FieldKind.TIME`), because that is the pair a person reads off a punch — and the
    platform assembles the instant when the form is confirmed, which keeps a browser's
    timezone out of an instant the server has to attribute to a Madrid business day.

    `CorrectionService.check_draft` is the check: the kind is a punch, the instant carries
    its day and has happened, the reason says why, and the day-and-kind pair identifies
    exactly one punch (or none, which is the forgotten punch the flow makes up).
    """
    if not _may(context, Action.ATTENDANCE_CORRECTION_OWN):
        return _refused(call)
    try:
        business_date = _date(call, "business_date")
        kind = _punch_kind(call)
        instant = _instant(call, "corrected_at")
        reason = _text(call, "reason")
    except BadArgument as bad:
        return _needs_details(call, (bad.field,))

    service = corrections(context.session)
    try:
        check = await service.check_draft(
            employee_id=context.principal.employee_id,
            business_date=business_date,
            kind=kind,
            corrected_at=instant,
            reason=reason,
        )
    except DomainError as refusal:
        return _invalid(call, refusal)

    local = check.corrected_at.astimezone(MADRID)
    form = PrefillForm(
        tool=call.name,
        entity=DraftEntity.ATTENDANCE_CORRECTION,
        title_key=_title_key(DraftEntity.ATTENDANCE_CORRECTION),
        title_es=_title(DraftEntity.ATTENDANCE_CORRECTION, "es"),
        title_en=_title(DraftEntity.ATTENDANCE_CORRECTION, "en"),
        submit_path=SUBMIT_PATH[DraftEntity.ATTENDANCE_CORRECTION],
        fields=(
            _date_field("business_date", check.business_date),
            _select(
                "kind",
                value=str(check.kind),
                options=tuple(
                    FieldOption(
                        value=str(item),
                        label_es=_option_label(str(item), "es"),
                        label_en=_option_label(str(item), "en"),
                    )
                    for item in sorted(PUNCH_EVENT_TYPES, key=str)
                ),
            ),
            PrefillField(
                name="corrected_at",
                label_key="agent.draft.field.corrected_at",
                kind=FieldKind.TIME,
                label_es=_label("corrected_at", "es"),
                label_en=_label("corrected_at", "en"),
                value=local.strftime("%H:%M"),
                hint_es=_hint("corrected_at", "es"),
                hint_en=_hint("corrected_at", "en"),
            ),
            PrefillField(
                name="reason",
                label_key="agent.draft.field.reason",
                kind=FieldKind.TEXTAREA,
                label_es=_label("reason", "es"),
                label_en=_label("reason", "en"),
                value=check.reason,
            ),
        ),
        facts={
            "business_date": check.business_date.isoformat(),
            "kind": str(check.kind),
            # The instant the check accepted, in UTC and in Madrid, so a reader can see
            # both what will be stored and what it looked like on the clock.
            "corrected_at": check.corrected_at.isoformat(),
            "corrected_at_local": local.isoformat(),
        },
    )
    return _drafted(call, form)


async def draft_timesheet(call: ToolCall, context: ToolContext) -> ToolResult:
    """工时表草稿: one filled-in time entry, or the reason it could not be one.

    One *entry* rather than a week: §6.2's row is `draft_timesheet` and the submission is
    `POST /timesheets/entries`, which writes one day's work on one task. A week is a grid a
    person fills in and this is the assistant filling in one cell of it.

    `TimesheetService.check_entry` is the check — the week is a Monday, the day is inside it,
    the minutes are a positive integer within a day, the employee exists, the week is inside
    the eight-week window, the week is not locked or filed, the day has room, and the
    project and task may be booked by *this* caller. The project read for the form's two
    selects is `ProjectService.recordable_projects`, which is the same `filter_for` the
    write path consumes — so a task the form offers is a task the submission accepts.
    """
    if not _may(context, Action.TIMESHEET_WRITE_OWN):
        return _refused(call)
    try:
        week_start = _date(call, "week_start")
        entry_date = _date(call, "entry_date")
        project_id = _uuid(call, "project_id")
        task_id = _uuid(call, "task_id")
        minutes = _integer(call, "minutes")
        note = _optional_text(call, "note")
    except BadArgument as bad:
        return _needs_details(call, (bad.field,))

    service = timesheets(context.session, context.principal)
    catalogue = projects(context.session)
    try:
        target = await service.check_entry(
            week_start,
            entry_date=entry_date,
            project_id=project_id,
            task_id=task_id,
            minutes=minutes,
        )
    except DomainError as refusal:
        return _invalid(call, refusal)

    form = PrefillForm(
        tool=call.name,
        entity=DraftEntity.TIMESHEET_ENTRY,
        title_key=_title_key(DraftEntity.TIMESHEET_ENTRY),
        title_es=_title(DraftEntity.TIMESHEET_ENTRY, "es"),
        title_en=_title(DraftEntity.TIMESHEET_ENTRY, "en"),
        # The submission's week is a query parameter, so the path carries it: a confirmed
        # form is posted to exactly this URL, and ticket 41 does not have to reconstruct it.
        submit_path=f"{SUBMIT_PATH[DraftEntity.TIMESHEET_ENTRY]}?week={week_start.isoformat()}",
        fields=(
            _date_field("entry_date", entry_date),
            _select(
                "project_id",
                value=str(target.project.id),
                options=await _project_options(catalogue, context),
            ),
            _select(
                "task_id",
                value=str(target.task.id),
                options=await _task_options(catalogue, target),
            ),
            PrefillField(
                name="minutes",
                label_key="agent.draft.field.minutes",
                kind=FieldKind.NUMBER,
                label_es=_label("minutes", "es"),
                label_en=_label("minutes", "en"),
                value=minutes,
                hint_es=_hint("minutes", "es"),
                hint_en=_hint("minutes", "en"),
            ),
            PrefillField(
                name="note",
                label_key="agent.draft.field.note",
                kind=FieldKind.TEXT,
                label_es=_label("note", "es"),
                label_en=_label("note", "en"),
                value=note,
                required=False,
            ),
        ),
        facts={
            "week_start": week_start.isoformat(),
            "week_end": (week_start + _WEEK).isoformat(),
            "project_code": target.project.code,
            "task_code": target.task.code,
            # The server's own answer about the entry — never a request field, and shown so
            # that the form can say what will be recorded.
            "is_billable": target.is_billable,
        },
    )
    return _drafted(call, form)


# --- arguments ----------------------------------------------------------------


def _text(call: ToolCall, name: str) -> str:
    value = call.arguments.get(name)
    if value is None or not str(value).strip():
        raise BadArgument(name, f"{call.name} needs a {name}")
    return str(value).strip()


def _optional_text(call: ToolCall, name: str) -> str | None:
    value = call.arguments.get(name)
    if value is None or not str(value).strip():
        return None
    return str(value).strip()


def _date(call: ToolCall, name: str) -> date:
    value = call.arguments.get(name)
    try:
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise BadArgument(name, f"{name}={value!r} is not a date") from error


def _instant(call: ToolCall, name: str) -> datetime:
    """A correction's instant, as the request carries it: with an offset, never naive.

    `datetime.fromisoformat` accepts a naive string too, and passing one on would be a
    crash inside the domain rather than an answer — `CorrectionService._timed` refuses it,
    which is the rule this reads, but the tool can say *which field* was wrong.
    """
    value = call.arguments.get(name)
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as error:
            raise BadArgument(name, f"{name}={value!r} is not an instant") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BadArgument(name, f"{name}={value!r} carries no timezone")
    return parsed


def _punch_kind(call: ToolCall) -> EventType:
    value = _text(call, "kind")
    try:
        return EventType(value)
    except ValueError as error:
        raise BadArgument("kind", f"kind={value!r} is not a punch") from error


def _uuid(call: ToolCall, name: str) -> UUID:
    value = call.arguments.get(name)
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as error:
        raise BadArgument(name, f"{name}={value!r} is not an id") from error


def _integer(call: ToolCall, name: str) -> int:
    value = call.arguments.get(name)
    if isinstance(value, bool) or value is None:
        raise BadArgument(name, f"{name}={value!r} is not a number")
    try:
        return int(str(value))
    except (TypeError, ValueError) as error:
        raise BadArgument(name, f"{name}={value!r} is not a number") from error


# --- the answers ---------------------------------------------------------------


def _may(context: ToolContext, action: Action) -> bool:
    """Whether the kernel permits the act this draft is a draft *of*.

    The same shape ticket 39's read-only tools use: the resource is an `EMPLOYEE` owned by
    the caller themselves, so the three `*_own` actions (`SELF_ONLY_ACTIONS`) are allowed
    only for the owner — a principal can neither read nor draft for somebody else, and the
    tool has no parameter that could name them.
    """
    resource = Resource(ResourceKind.EMPLOYEE, owner_employee_id=context.principal.employee_id)
    return can(context.principal, action, resource).allowed


def _refused(call: ToolCall) -> ToolResult:
    return ToolResult(tool=call.name, outcome=ToolOutcome.REFUSED)


def _needs_details(call: ToolCall, fields: Sequence[str]) -> ToolResult:
    """The form cannot be filled: say which fields, and draft nothing."""
    return ToolResult(
        tool=call.name,
        outcome=ToolOutcome.INVALID,
        data={
            "reason": "needs_details",
            "message_key": NEEDS_DETAILS_KEY,
            "fields": list(fields),
        },
    )


def _invalid(call: ToolCall, refusal: DomainError) -> ToolResult:
    """The submission would refuse this, so there is no draft — and here is why.

    The catalogue key travels rather than a sentence: the domain error already has wording
    in both languages (`app/core/messages.py`), and the client renders the reader's own. The
    `detail` travels beside it for the operator, exactly as the API's error envelope carries
    it — that is what an engineer reads when the sentence is not specific enough.
    """
    code = ErrorCode(refusal.code.value)
    return ToolResult(
        tool=call.name,
        outcome=ToolOutcome.INVALID,
        data={
            "reason": "refused",
            "error_code": code.value,
            "message_key": definition_of(code).message_key,
            "detail": refusal.detail or "",
        },
    )


def _drafted(call: ToolCall, form: PrefillForm) -> ToolResult:
    return ToolResult(tool=call.name, outcome=ToolOutcome.OK, data=form.as_dict())


# --- form furniture -----------------------------------------------------------


def _label(field: str, language: str) -> str:
    """One field's label, read from the catalogue. See `render.py` for why it travels here.

    The client draws an editable form, and the wording lives in `app/core/messages.py` —
    the one place this project keeps user-facing sentences. Sending the key alone would
    make every client keep a second copy of it; sending both languages, as `ToolAnswer`
    already does, lets the reader's own be chosen without one.
    """
    return MESSAGES[language][f"agent.draft.field.{field}"]


def _option_label(value: str, language: str) -> str:
    """One select choice's wording. The punch kinds are the only values that need one."""
    return MESSAGES[language][f"agent.draft.option.{value}"]


def _title(entity: DraftEntity, language: str) -> str:
    """One form's heading, in one language, from the catalogue.

    The heading travels with the form for the reason the field labels do: a client that
    received only `title_key` would keep a second copy of the wording, and the two would
    drift the first time somebody reworded one of them.
    """
    return MESSAGES[language][_title_key(entity)]


def _title_key(entity: DraftEntity) -> str:
    """The catalogue key a form's heading is read from.

    Derived from the entity rather than written out at each tool, because the two are the
    same fact: a form whose `title_key` named another entity's wording is a heading that
    describes the wrong document, and the first draft tool written that way proved it.
    """
    return f"agent.draft.title.{entity}"


def _hint(field: str, language: str) -> str | None:
    key = f"agent.draft.hint.{field}"
    return MESSAGES[language].get(key)


def _select(name: str, *, value: str, options: Sequence[FieldOption]) -> PrefillField:
    return PrefillField(
        name=name,
        label_key=f"agent.draft.field.{name}",
        kind=FieldKind.SELECT,
        label_es=_label(name, "es"),
        label_en=_label(name, "en"),
        value=value,
        options=tuple(options),
    )


def _date_field(name: str, value: date) -> PrefillField:
    return PrefillField(
        name=name,
        label_key=f"agent.draft.field.{name}",
        kind=FieldKind.DATE,
        label_es=_label(name, "es"),
        label_en=_label(name, "en"),
        value=value.isoformat(),
    )


async def _project_options(
    catalogue: ProjectService, context: ToolContext
) -> tuple[FieldOption, ...]:
    """The projects this caller may book, as select choices.

    `recordable_projects` is the list form of the rule the check applies, from the same
    `filter_for` — so a project the form offers is a project the submission accepts, and a
    picker and a write path cannot disagree.
    """
    page = await catalogue.recordable_projects(context.principal, ProjectQuery(limit=_OPTIONS))
    return tuple(
        FieldOption(
            value=str(project.id),
            label_es=f"{project.code} · {project.name_es}",
            label_en=f"{project.code} · {project.name_en}",
        )
        for project in page.items
    )


async def _task_options(
    catalogue: ProjectService, target: RecordTarget
) -> tuple[FieldOption, ...]:
    """The tasks of the draft's own project, so the second select offers what the first did.

    Only the named project's tasks: a task id from another project is refused by
    `resolve_record_target` (the task has to belong to the project), and offering one would
    be offering a refusal. Inactive tasks are left out for the same reason — they are the
    other half of what that check refuses.
    """
    tasks = await catalogue.list_tasks(target.project.id, include_inactive=False)
    return tuple(
        FieldOption(
            value=str(task.id),
            label_es=f"{task.code} · {task.name_es}",
            label_en=f"{task.code} · {task.name_en}",
        )
        for task in tasks
    )


def _leave_facts(check: LeaveRequestCheck) -> dict[str, Any]:
    """What the leave check answered, for the form to show beside the dates.

    A count and two dates, never a sentence: the same shape `render.py` renders from. The
    split per year is here because a request across the boundary is charged to two balances
    and the employee should see that before confirming it.
    """
    return {
        "business_days_count": check.business_days_count,
        "working_days": [day.isoformat() for day in check.working_days],
        "allocations": [
            {"year": year, "days": days} for year, days in sorted(check.counts.items())
        ],
        "requires_attachment": check.leave_type.requires_attachment,
    }


_WEEK = timedelta(days=6)

#: How many projects a form's select carries. A page rather than every project in the
#: company: a select with two hundred entries is not a control anybody uses, and the project
#: the draft names is in it because `recordable_projects` lists what the caller may book.
_OPTIONS = 100


#: 草稿 §6.2's second half, keyed by the name a caller uses. A plain dict here and a
#: `MappingProxyType` in `registry.py`, for the reason `readonly.py` records: this is the
#: literal, that is the registry.
DRAFT_TOOLS: dict[str, Tool] = {
    "draft_leave_request": Tool(
        name="draft_leave_request",
        kind=ToolKind.DRAFT,
        summary="A filled-in leave request for the caller to review and confirm",
        parameters=(
            "leave_type",
            "start_date",
            "end_date",
            "attachment_reference",
        ),
        run=draft_leave_request,
    ),
    "draft_attendance_correction": Tool(
        name="draft_attendance_correction",
        kind=ToolKind.DRAFT,
        summary="A filled-in correction for one of the caller's own punches",
        parameters=("business_date", "kind", "corrected_at", "reason"),
        run=draft_attendance_correction,
    ),
    "draft_timesheet": Tool(
        name="draft_timesheet",
        kind=ToolKind.DRAFT,
        summary="One filled-in time entry for the caller's own week",
        parameters=("week_start", "entry_date", "project_id", "task_id", "minutes", "note"),
        run=draft_timesheet,
    ),
}


__all__ = [
    "DRAFT_TOOLS",
    "NEEDS_DETAILS_KEY",
    "SUBMIT_PATH",
    "BadArgument",
    "draft_attendance_correction",
    "draft_leave_request",
    "draft_timesheet",
]
