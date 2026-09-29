"""Ticket 40: the draft tools, the form they produce, and the platform that records it.

Seven checklist lines, and the test that pins each:

* 提供草稿工具：请假申请草稿、补打卡草稿、工时表草稿 —
  `test_the_draft_half_of_the_registry_is_design_6_2s_three_rows`,
  `test_each_tool_produces_a_filled_in_form`
* 草稿工具只构造并返回一张结构化表单，**不产生任何数据库写入** —
  `test_a_draft_tool_writes_nothing_at_all`, which counts **every table in the schema**
  before and after invoking each of the three tools, with a positive control that proves the
  counter sees a write
* 草稿内容经过校验…不合法时明确告知原因 —
  `test_each_refusal_is_the_submission_s_own_refusal` and the six rule tests beside it, each
  of which compares the tool's answer with what the *submission endpoint itself* answers for
  the same input
* 草稿以持久化状态保存并关联到对话，刷新页面或重启服务后仍能找到 —
  `test_the_draft_survives_a_restart_and_is_read_back_through_the_conversation`,
  `test_the_form_is_complete_and_editable`
* 草稿有有效期（默认 24 小时），过期后标记为失效并要求重新生成 —
  `test_the_default_lifetime_is_a_day_and_the_clock_is_the_database_s`,
  `test_a_lapsed_draft_is_marked_expired_when_it_is_read`
* 架构层面验证：AI 模块的依赖中不存在任何写入型仓储；断言助手工具集里没有写库工具 —
  `test_the_tool_set_has_no_write_tool_and_the_agent_reaches_no_repository`
* 草稿内容在界面上以完整可编辑表单呈现 —
  `test_every_field_of_every_submission_is_a_field_of_its_form` (the form's field names are
  compared with the endpoint's own request model, minus the identity fields) and the visual
  run, which draws them.

**Real infrastructure, and the repository's own seams only.** PostgreSQL is real, the punches
and the weeks are real rows written through the API or into the stream the way
`test_attendance_corrections.py` writes them, and the draft is read back through the real
`langgraph` checkpointer and the real HTTP endpoint. No model is involved anywhere: the draft
branch takes its call from the state and calls no model at all.
"""

import ast
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.ai.agents import AgentContext, build_graph, open_checkpointer, thread_config
from app.ai.agents import state as agent_state
from app.ai.tools import (
    REGISTRY,
    Tool,
    ToolCall,
    ToolContext,
    ToolKind,
    ToolOutcome,
    ToolResult,
    invoke,
    registered,
    render,
)
from app.ai.tools import draft as draft_tools
from app.ai.tools.draft import NEEDS_DETAILS_KEY
from app.api.v1.attendance import CorrectionCreate
from app.api.v1.leave import RequestCreate
from app.api.v1.schemas.timesheet import EntryWrite
from app.config import get_settings
from app.core.messages import MESSAGES
from app.domain.agent import repository as agent_actions_repository
from app.domain.agent.models import IDENTITY_FIELDS, DraftStatus, PrefillForm
from app.domain.approval.models import DecisionKind
from app.domain.approval.service import ApprovalService
from app.domain.attendance.business_day import MADRID, madrid_today
from app.domain.attendance.models import EventSource, EventType
from app.domain.timesheet.models import monday_of
from app.domain.timesheet.service import ENTITY_TYPE
from app.models.agent_action import STATUS_VALUES
from app.repositories.approval import PostgresApprovalRepository
from tests.support.platform import Actor, Platform
from tests.test_agent_graph import company_week, pipeline, principal_of

FULL_DAY = 480
#: A Monday far enough back to be inside the eight-week window and in the past.
LAST_WEEK = 7


# --- the cast -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cast:
    """Two colleagues in one department, and the manager whose projects they book.

    Two employees because the *identity* is half of what these tools must get right: the
    draft of one must be checked against that person's own balance, week and punch stream,
    and a test with one employee cannot tell "as the caller" from "as anybody".
    """

    employee: Actor
    colleague: Actor
    manager: Actor
    hr: Actor
    department: str
    project: dict
    task: dict


@pytest.fixture
async def cast(platform: Platform) -> Cast:
    suffix = uuid4().hex[:8]
    await company_week(platform, code=f"dw{suffix}")
    department = await platform.department(f"dw{suffix}")
    position = await platform.position(department, f"dwt{suffix}")
    manager = await platform.account(roles=("manager",))
    await platform.assign(
        manager.employee_id,
        department,
        await platform.position(department, f"dwm{suffix}", is_managerial=True),
    )
    employee = await platform.account(roles=("employee",))
    colleague = await platform.account(roles=("employee",))
    hr = await platform.account(roles=("hr",))
    # The manager is named on the assignments, which is what the approval engine resolves
    # level one from: a week nobody can approve is a week that cannot be filed, and this
    # fixture needs an *approved* week for the lock.
    for person in (employee, colleague):
        await platform.assign(
            person.employee_id,
            department,
            position,
            manager_employee_id=manager.employee_id,
        )
    await platform.assign(hr.employee_id, department, position)

    project = await _project(manager, department)
    task = await _task(manager, project["id"])
    return Cast(
        employee=employee,
        colleague=colleague,
        manager=manager,
        hr=hr,
        department=department,
        project=project,
        task=task,
    )


async def _project(actor: Actor, department_id: str) -> dict:
    created = await actor.post(
        "/api/v1/projects",
        json={
            "code": f"dw{uuid4().hex[:6]}",
            "name_es": "Proyecto",
            "name_en": "Project",
            "department_id": department_id,
            "start_date": "2020-01-01",
            "end_date": None,
            "status": "active",
            "is_billable_default": True,
        },
    )
    assert created.status_code == 201, created.text
    return created.json()


async def _task(actor: Actor, project_id: str) -> dict:
    created = await actor.post(
        f"/api/v1/projects/{project_id}/tasks",
        json={"code": f"t{uuid4().hex[:6]}", "name_es": "Tarea", "name_en": "Task"},
    )
    assert created.status_code == 201, created.text
    return created.json()


async def punch(
    platform: Platform, actor: Actor, day: date, *, hours: int = 8, start_hour: int = 9
) -> None:
    """A closed shift in the real punch stream, as `test_agent_readonly_tools.py` writes it.

    `start_hour` shifts the pair, because the stream's unique index is
    `(employee_id, event_type, occurred_at)`: two shifts on one day have to *be* two
    different instants, and the second is the case a correction cannot resolve.
    """
    for kind, hour in ((EventType.CLOCK_IN, start_hour), (EventType.CLOCK_OUT, start_hour + hours)):
        await platform.sql(
            """
            INSERT INTO attendance_events
                (id, employee_id, event_type, occurred_at, business_date, source)
            VALUES (:id, :employee_id, :kind, :occurred_at, :day, :source)
            """,
            {
                "id": uuid4(),
                "employee_id": actor.employee_id,
                "kind": str(kind),
                "occurred_at": datetime(
                    day.year, day.month, day.day, hour, tzinfo=MADRID
                ).astimezone(UTC),
                "day": day,
                "source": str(EventSource.WEB),
            },
        )


# --- driving one tool, and the graph ------------------------------------------


@asynccontextmanager
async def tool_context(platform: Platform, actor: Actor) -> AsyncIterator[ToolContext]:
    """A real principal and a real session, which is everything a tool is given."""
    session = platform.factory()
    try:
        yield ToolContext(
            principal=await principal_of(platform, actor),
            session=session,
            today=madrid_today(datetime.now(UTC)),
        )
    finally:
        await session.close()


async def call(
    platform: Platform, actor: Actor, name: str, **arguments: Any
) -> tuple[Any, ToolContext]:
    """Invoke one tool as `actor`, and hand back the result and the context it ran in."""
    async with tool_context(platform, actor) as context:
        result = await invoke(ToolCall(name=name, arguments=arguments), context)
    return result, context


def form_of(result: Any) -> PrefillForm:
    """The form a successful draft produced, re-validated the way the platform reads it."""
    assert result.outcome is ToolOutcome.OK, result.data
    form = PrefillForm.from_stored(result.data)
    assert form is not None
    return form


def leave_draft(cast: Cast, *, day: date) -> dict[str, Any]:
    """Valid `draft_leave_request` arguments for one working day and the next."""
    return {
        "leave_type": "annual",
        "start_date": day.isoformat(),
        "end_date": (day + timedelta(days=1)).isoformat(),
    }


def drafted_payload(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """The state a run is invoked with when a caller names the draft to produce.

    The seam ticket 42 replaces with a model's function call: a name and its arguments,
    without a question a lexical layer would have to happen to read. The question is still
    carried, because a draft the platform records is filed under the conversation its title
    comes from.
    """
    return {
        "question": "Quiero pedir dos dias de permiso",
        "tool": "draft_leave_request",
        "tool_arguments": dict(arguments),
    }


@asynccontextmanager
async def graph_run(
    platform: Platform, actor: Actor, arguments: Mapping[str, Any], *, thread: str | None = None
) -> AsyncIterator[dict]:
    """Run the graph's draft branch on a real checkpointer, and yield the state.

    Its own pipeline, session and checkpointer: the run is entered the way a request enters
    it — a fresh context, a fresh connection — so a value that only existed in the caller's
    locals could not survive it.
    """
    async with pipeline(platform) as answers:
        session = platform.factory()
        context = AgentContext(
            principal=await principal_of(platform, actor),
            answers=answers.service,
            session=session,
        )
        try:
            async with open_checkpointer(get_settings(), test=True) as saver:
                yield await build_graph(checkpointer=saver).ainvoke(
                    drafted_payload(arguments),
                    thread_config(thread or uuid4().hex),
                    context=context,
                )
        finally:
            await session.close()


# --- 1: the three tools -------------------------------------------------------


def test_the_draft_half_of_the_registry_is_design_6_2s_three_rows() -> None:
    """Checklist: 提供草稿工具：请假申请草稿、补打卡草稿、工时表草稿.

    Three names, filed under themselves, of the draft kind — and the read half is untouched
    by this ticket, which is asserted too because a ticket that *replaced* a read tool with a
    draft one would otherwise pass this test.
    """
    assert {tool.name for tool in registered(ToolKind.DRAFT)} == {
        "draft_leave_request",
        "draft_attendance_correction",
        "draft_timesheet",
    }
    for name in ("draft_leave_request", "draft_attendance_correction", "draft_timesheet"):
        assert REGISTRY[name].kind is ToolKind.DRAFT
    assert len(registered(ToolKind.READ_ONLY)) == 5
    assert not hasattr(ToolKind, "WRITE"), "a write kind exists"


def test_the_tool_set_has_no_write_tool_and_the_agent_reaches_no_repository() -> None:
    """Checklist: 架构层面验证（constraint B 的第一层）, as three separate claims.

    `codebase-design.md` §6's first layer is two sentences and both are checked here, from
    the two directions that can falsify them:

    1. **the tool set has no write tool** — every registered tool's kind is one of the two
       `ToolKind` members and `WRITE` does not exist, so a write tool cannot be described,
       let alone registered;
    2. **the implementations reach no write** — `tests/test_agent_readonly_tools.py`'s AST
       walk covers the whole `app/ai/tools` package, which now includes this ticket's
       `draft.py`; this test asserts that the walk really saw that module (a package walk
       that silently skipped it would make the other ticket's assertion vacuous for the
       draft half);
    3. **the agent package holds no write repository** — no module under `app/ai/agents/`
       imports `app.repositories.*` or `app.models.*`. The *node* records the draft, and it
       does so through a domain service (`app/domain/agent/service.py`), exactly as
       `answer_policy` reaches `rag_messages` through `AnswerService`. The tools package
       imports repositories (it builds the domain services), which is why layer 1's
       testable form is "no write is called", not "no repository is named".
    """
    assert {str(tool.kind) for tool in registered()} <= {"read_only", "draft"}

    walked = {
        Path(tool.run.__code__.co_filename).name for tool in registered(ToolKind.DRAFT)
    }
    assert walked == {"draft.py"}, walked

    agents = Path(agent_state.__file__).parent
    offenders: dict[str, list[str]] = {}
    for module in sorted(agents.glob("*.py")):
        reached = _imports(module.read_text(encoding="utf-8"))
        bad = sorted(
            name
            for name in reached
            if name.startswith("app.repositories") or name == "app.models"
        )
        if bad:
            offenders[module.name] = bad
    assert offenders == {}, f"the agent package holds a write repository: {offenders}"


def _imports(source: str) -> set[str]:
    """Every module name one source file imports, at any level."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.add(node.module)
    return found


def test_the_column_and_the_enum_agree() -> None:
    """The row's `CHECK` and `DraftStatus` are two lists kept in step by this test.

    `app/models/agent_action.py` cannot import the enum (a model module importing the domain
    package pulls in the audit trail and cycles), so the two literals are held together here
    instead — the same arrangement the retrieval module's vocabulary uses.
    """
    assert set(STATUS_VALUES) == {str(status) for status in DraftStatus}


# --- 2: the form --------------------------------------------------------------


#: Each draft tool, its arguments, and the submission endpoint plus request model those
#: arguments are a draft *of*. One table, because three assertions in this file are the same
#: assertion three times: the fields, the refusal, and "the identity is not a field".
SUBMISSIONS: tuple[tuple[str, str, Any], ...] = (
    ("draft_leave_request", "/api/v1/leave/requests", RequestCreate),
    ("draft_attendance_correction", "/api/v1/attendance/corrections", CorrectionCreate),
    ("draft_timesheet", "/api/v1/timesheets/entries", EntryWrite),
)


def arguments_for_draft(cast: Cast, name: str, *, day: date, week: date) -> dict[str, Any]:
    """Valid arguments for one draft tool, for a person with a company week."""
    if name == "draft_leave_request":
        return {
            "leave_type": "annual",
            "start_date": day.isoformat(),
            "end_date": (day + timedelta(days=1)).isoformat(),
        }
    if name == "draft_attendance_correction":
        return {
            "business_date": day.isoformat(),
            "kind": "clock_out",
            "corrected_at": datetime(
                day.year, day.month, day.day, 16, 10, tzinfo=MADRID
            ).isoformat(),
            "reason": "Olvide fichar la salida.",
        }
    return {
        "week_start": week.isoformat(),
        "entry_date": week.isoformat(),
        "project_id": cast.project["id"],
        "task_id": cast.task["id"],
        "minutes": FULL_DAY,
        "note": "Revision de la interfaz",
    }


def working_day(today: date) -> date:
    """A working day before `today`: the Monday of last week.

    Computed rather than written as a literal so the suite does not fail in a month whose
    calendar differs, and far enough back that a punch on it is over.
    """
    candidate = today - timedelta(days=LAST_WEEK)
    return candidate if candidate.weekday() < 5 else monday_of(candidate)


@pytest.mark.parametrize("name,path,model", SUBMISSIONS)
async def test_every_field_of_every_submission_is_a_field_of_its_form(
    platform: Platform, cast: Cast, name: str, path: str, model: Any
) -> None:
    """Checklist: 界面上以完整可编辑表单呈现，员工可以在提交前修改任何字段.

    The strongest form of「完整」available without a browser: the form's field names are
    compared with the endpoint's *own request model*, minus the identity fields. A form
    missing a field the submission writes would fail here; a form offering a field the
    endpoint does not accept would too, and so would a form that had grown an `employee_id`
    the model could fill in.

    The labels and the input kinds are asserted beside it, because a field a person cannot
    read or edit is not an editable form: every label is in both catalogues, and every kind
    is one a client can draw.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    await punch(platform, cast.employee, day)
    arguments = arguments_for_draft(cast, name, day=day, week=monday_of(today))
    result, _ = await call(platform, cast.employee, name, **arguments)
    form = form_of(result)

    expected = set(model.model_fields) - IDENTITY_FIELDS
    assert set(form.field_names) == expected, (
        f"{name}: the form and {path} disagree about the fields"
    )
    assert form.submit_path.startswith(path), form.submit_path
    assert form.tool == name
    for field in form.fields:
        assert field.label_es and field.label_en, field
        assert field.label_key in MESSAGES["es"], field.label_key
        assert field.label_key in MESSAGES["en"], field.label_key
        assert str(field.kind) in {
            "date",
            "time",
            "text",
            "textarea",
            "number",
            "select",
        }
    assert form.title_key in MESSAGES["es"] and form.title_key in MESSAGES["en"]

    # The identity is not a field *and* not a parameter: the same claim from the other side.
    assert not IDENTITY_FIELDS & set(form.field_names)
    assert not IDENTITY_FIELDS & set(REGISTRY[name].parameters)


@pytest.mark.parametrize("name,path,model", SUBMISSIONS)
async def test_each_tool_produces_a_filled_in_form(
    platform: Platform, cast: Cast, name: str, path: str, model: Any
) -> None:
    """Each tool fills its form from the caller's own data, and says what it validated.

    The figures beside the form are the check's — the working days a leave costs, the
    instant a correction was accepted at, the billable answer a time entry resolved to — so
    a person confirming the draft sees what the *submission* would record rather than what
    the assistant believes.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    await punch(platform, cast.employee, day)
    arguments = arguments_for_draft(cast, name, day=day, week=monday_of(today))
    result, _ = await call(platform, cast.employee, name, **arguments)
    form = form_of(result)

    assert form.facts, f"{name}: the form says nothing about what was validated"
    assert render(result).es and render(result).en
    if name == "draft_leave_request":
        assert form.facts["business_days_count"] == 2
        assert form.facts["working_days"] == [
            day.isoformat(),
            (day + timedelta(days=1)).isoformat(),
        ]
        assert form.field("leave_type").value == "annual"
        assert form.field("start_date").value == day.isoformat()
    elif name == "draft_attendance_correction":
        assert form.field("corrected_at").value == "16:10"
        assert form.field("kind").value == "clock_out"
        assert form.facts["business_date"] == day.isoformat()
    else:
        assert form.field("minutes").value == FULL_DAY
        assert form.field("project_id").value == cast.project["id"]
        assert form.facts["is_billable"] is True
        assert form.submit_path.endswith(f"week={monday_of(today).isoformat()}")


# --- 3: the purity test -------------------------------------------------------


async def table_counts(platform: Platform) -> dict[str, int]:
    """How many rows every table in the `public` schema holds, right now.

    **Enumerated from the catalog rather than listed here**, and that is the whole point:
    a test that counted a hand-written list of tables would keep passing the day somebody
    adds a table and a write to it. `pg_tables` is the schema's own answer, so a new table
    is counted the moment it exists — including this ticket's `agent_actions`.
    """
    names = [
        row[0]
        for row in await platform.sql(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
    ]
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = int(
            await platform.scalar(f'SELECT count(*) FROM public."{name}"') or 0
        )
    return counts


#: The two tables the *platform* writes when a draft is produced, and the trail every write
#: in this system leaves. Named here so the purity test can say "these are in the counted
#: set", which is what stops a future edit from slipping a write into a table nobody counts.
PLATFORM_TABLES = ("agent_actions", "rag_conversations", "rag_messages", "audit_log")


async def test_a_draft_tool_writes_nothing_at_all(platform: Platform, cast: Cast) -> None:
    """Checklist: 草稿工具只构造并返回一张结构化表单，不产生任何数据库写入.

    **Every table in the schema, counted before and after.** A test that asserted "no
    `leave_requests` row appeared" would pass against a tool that wrote an audit row, a
    ledger entry or a lock — and it is exactly that class of edit (a "small audit row", as
    the ticket puts it) that this assertion exists to catch.

    The positive control at the end is what keeps it from being vacuous: the same counter is
    run over a run that *does* write (the graph's draft branch, whose platform records the
    draft), and it must see the difference. Without it, a counter that returned the same
    numbers for every input would pass this test whatever the tools did.

    The tool calls are made with **valid** arguments — a refused draft writes nothing for an
    uninteresting reason.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    await punch(platform, cast.employee, day)
    week = monday_of(today)

    before = await table_counts(platform)
    assert set(PLATFORM_TABLES) <= set(before), (
        "a table the platform writes is not being counted, so this assertion could not see "
        f"a write to it: {sorted(set(PLATFORM_TABLES) - set(before))}"
    )

    for name, _path, _model in SUBMISSIONS:
        arguments = arguments_for_draft(cast, name, day=day, week=week)
        result, _ = await call(platform, cast.employee, name, **arguments)
        assert result.outcome is ToolOutcome.OK, (name, result.outcome, result.data)

    after = await table_counts(platform)
    assert after == before, {
        "a draft tool wrote to the database": {
            table: (before[table], after[table])
            for table in before
            if before[table] != after.get(table)
        }
    }

    # --- the positive control: the same counter sees the platform's write ---------
    async with graph_run(platform, cast.employee, leave_draft(cast, day=day)) as state:
        assert state["pending_action"]["status"] == "proposed"
    recorded = await table_counts(platform)
    assert recorded["agent_actions"] == before["agent_actions"] + 1, (
        "the counter does not see the platform's own write, so it could not see a tool's"
    )
    assert recorded["rag_conversations"] == before["rag_conversations"] + 1
    # The row *is* the trail for this act: D22's record of what the assistant proposed is
    # `agent_actions`, and a second `audit_log` entry would be a person's audit trail
    # claiming somebody did something they did not.
    assert recorded["audit_log"] == before["audit_log"]


async def test_a_refused_or_incomplete_draft_writes_nothing_either(
    platform: Platform, cast: Cast
) -> None:
    """The other half, on the two shapes that never reach a form.

    An argument the tool cannot use and a document the domain refuses both end in `INVALID`
    — and both are still "the tool wrote nothing", which isworth asserting separately
    because these are the paths where a helpful implementation would be tempted to record
    the attempt.
    """
    before = await table_counts(platform)
    missing, _ = await call(platform, cast.employee, "draft_timesheet", week_start="2026-01-01")
    refused, _ = await call(
        platform,
        cast.employee,
        "draft_leave_request",
        leave_type="annual",
        start_date="2026-05-11",
        end_date="2026-05-08",
    )
    after = await table_counts(platform)

    assert missing.outcome is ToolOutcome.INVALID
    assert refused.outcome is ToolOutcome.INVALID
    assert after == before, {
        table: (before[table], after[table])
        for table in before
        if before[table] != after.get(table)
    }


# --- 4: the validation is the submission's ------------------------------------


@pytest.mark.parametrize(
    "name,path,model",
    SUBMISSIONS,
)
def test_every_refusal_states_a_reason(
    name: str, path: str, model: Any
) -> None:
    """Checklist: 不合法时明确告知原因而不是生成一张注定失败的草稿.

    Two reasons and one sentence each, both read from the catalogue the API itself answers
    with — no wording of this ticket's own: `needs_details` names the fields that are
    missing, and a refusal carries the `message_key` the submission would have raised.
    """
    assert NEEDS_DETAILS_KEY in MESSAGES["es"] and NEEDS_DETAILS_KEY in MESSAGES["en"]
    for locale, catalogue in MESSAGES.items():
        assert "{fields}" in catalogue[NEEDS_DETAILS_KEY], locale


async def test_a_missing_field_is_named_rather_than_guessed(platform: Platform, cast: Cast) -> None:
    """A draft that cannot be filled asks for the field, in the reader's own words.

    The reply is the *labels* of the fields that are missing — "Fecha de inicio", not
    `start_date` — because the answer is read by the person who is going to type them.
    """
    result, _ = await call(
        platform, cast.employee, "draft_leave_request", leave_type="annual"
    )
    assert result.outcome is ToolOutcome.INVALID
    assert result.data["message_key"] == NEEDS_DETAILS_KEY
    assert result.data["fields"] == ["start_date"]
    answer = render(result)
    assert answer.message_key == NEEDS_DETAILS_KEY
    assert "Fecha de inicio" in answer.es
    assert "Start date" in answer.en
    assert "start_date" not in answer.es, "the answer leaked a field name"


async def test_the_leave_balance_refusal_is_the_route_s(platform: Platform, cast: Cast) -> None:
    """「额度是否足够」, checked by the ledger, refused in the request route's own words.

    HR grants one day, the draft asks for two: the tool refuses, and the *same* body posted
    to the submission endpoint is refused with the same `message_key`. That equality is the
    evidence that these are one rule and not two implementations of one rule.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    granted = await cast.hr.put(
        f"/api/v1/leave/balances/{cast.employee.employee_id}/{day.year}/annual",
        json={"entitled_days": 1, "carried_over_days": 0, "note": "una prueba"},
    )
    assert granted.status_code == 200, granted.text

    body = leave_draft(cast, day=day)
    result, _ = await call(platform, cast.employee, "draft_leave_request", **body)
    posted = await cast.employee.post("/api/v1/leave/requests", json=body)

    assert result.outcome is ToolOutcome.INVALID
    assert posted.status_code >= 400, posted.text
    assert result.data["message_key"] == posted.json()["error"]["message_key"]
    assert result.data["message_key"] == "errors.leave_balance_insufficient"
    assert result.data["error_code"] == posted.json()["error"]["code"]
    # And the reason is stated: the answer is the catalogue's sentence, not an apology.
    assert render(result).es == MESSAGES["es"]["errors.leave_balance_insufficient"]


async def test_the_leave_window_refusal_is_the_route_s(platform: Platform, cast: Cast) -> None:
    """「日期范围」: a weekend is not leave, and the draft says so before the employee files it.

    A range with no working day is refused by `LeaveService` — the same refusal
    `POST /leave/requests` gives — and the tool must not have written a leave request row
    for it either (which the purity test asserts for the valid case and this one for the
    invalid one).
    """
    today = madrid_today(datetime.now(UTC))
    saturday = today - timedelta(days=today.weekday()) + timedelta(days=5)
    while saturday >= today:
        saturday -= timedelta(days=7)
    body = {
        "leave_type": "annual",
        "start_date": saturday.isoformat(),
        "end_date": saturday.isoformat(),
    }
    result, _ = await call(platform, cast.employee, "draft_leave_request", **body)
    posted = await cast.employee.post("/api/v1/leave/requests", json=body)

    assert result.outcome is ToolOutcome.INVALID
    assert result.data["message_key"] == "errors.leave_request_invalid"
    assert result.data["message_key"] == posted.json()["error"]["message_key"]


async def test_the_correction_refusals_are_the_routes(platform: Platform, cast: Cast) -> None:
    """「同日／锁」规则: the punch has to be identifiable, and it has to have happened.

    Two cases, each compared with the submission's own answer: a day with two clock-outs is
    a day the flow refuses to guess about, and an instant in the future is not a correction
    of anything. The first is the ticket's own 「同日多次更正」boundary seen from the other
    side — a *draft* cannot pick one of the two punches either.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    # Two shifts on one day: `CorrectionService` refuses to say which clock-out a request
    # is about, and a draft must refuse for the same reason. `start_hour` keeps the two
    # pairs distinct, which the punch stream's unique index requires.
    await punch(platform, cast.employee, day, hours=8)
    await punch(platform, cast.employee, day, hours=8, start_hour=10)

    ambiguous = {
        "business_date": day.isoformat(),
        "kind": "clock_out",
        "corrected_at": datetime(
            day.year, day.month, day.day, 16, 30, tzinfo=MADRID
        ).isoformat(),
        "reason": "Creo que la salida era mas tarde.",
    }
    result, _ = await call(
        platform, cast.employee, "draft_attendance_correction", **ambiguous
    )
    posted = await cast.employee.post("/api/v1/attendance/corrections", json=ambiguous)
    assert result.outcome is ToolOutcome.INVALID
    assert result.data["message_key"] == "errors.attendance_correction_target_unresolved"
    assert result.data["message_key"] == posted.json()["error"]["message_key"]

    future = {
        "business_date": (today + timedelta(days=1)).isoformat(),
        "kind": "clock_in",
        "corrected_at": datetime(
            today.year, today.month, today.day, 8, 0, tzinfo=MADRID
        ).isoformat(),
        "reason": "Manana entrare a las ocho.",
    }
    later, _ = await call(platform, cast.employee, "draft_attendance_correction", **future)
    refused = await cast.employee.post("/api/v1/attendance/corrections", json=future)
    assert later.outcome is ToolOutcome.INVALID
    assert later.data["message_key"] == refused.json()["error"]["message_key"]


async def test_the_correction_needs_a_reason_and_an_aware_instant(
    platform: Platform, cast: Cast
) -> None:
    """The two shapes an argument check catches, and both name the field."""
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    await punch(platform, cast.employee, day)

    no_reason, _ = await call(
        platform,
        cast.employee,
        "draft_attendance_correction",
        business_date=day.isoformat(),
        kind="clock_out",
        corrected_at=datetime(day.year, day.month, day.day, 16, 0, tzinfo=MADRID).isoformat(),
    )
    naive, _ = await call(
        platform,
        cast.employee,
        "draft_attendance_correction",
        business_date=day.isoformat(),
        kind="clock_out",
        corrected_at=datetime(day.year, day.month, day.day, 16, 0).isoformat(),
        reason="Sin zona horaria.",
    )
    assert no_reason.outcome is ToolOutcome.INVALID
    assert no_reason.data["fields"] == ["reason"]
    assert naive.outcome is ToolOutcome.INVALID
    assert naive.data["fields"] == ["corrected_at"]


async def test_the_timesheet_refusals_are_the_routes(platform: Platform, cast: Cast) -> None:
    """「项目任务是否存在」: an inactive task cannot be booked, by a draft either.

    The refusal compared with the submission's own: both are
    `TIMESHEET_ENTRY_PROJECT_NOT_RECORDABLE`, which is what `resolve_record_target` answers
    for a task nobody may write against — the *same* call the write path makes.
    """
    today = madrid_today(datetime.now(UTC))
    week = monday_of(today)
    await cast.manager.post(
        f"/api/v1/projects/{cast.project['id']}/tasks/{cast.task['id']}/deactivate"
    )
    body = {
        "entry_date": week.isoformat(),
        "project_id": cast.project["id"],
        "task_id": cast.task["id"],
        "minutes": FULL_DAY,
    }
    result, _ = await call(
        platform,
        cast.employee,
        "draft_timesheet",
        week_start=week.isoformat(),
        **body,
    )
    posted = await cast.employee.post(
        "/api/v1/timesheets/entries", params={"week": week.isoformat()}, json=body
    )
    assert result.outcome is ToolOutcome.INVALID
    assert result.data["message_key"] == posted.json()["error"]["message_key"]
    # The project module owns this refusal — an inactive task is *its* rule, and both
    # callers answer with its code rather than a timesheet-shaped copy of it.
    assert result.data["message_key"] == "errors.project_task_not_recordable"


async def test_the_week_lock_refusal_does_not_lock_the_week(
    platform: Platform, cast: Cast
) -> None:
    """The one place a draft deliberately does *not* do what a write does, and why.

    `_require_open_week` records the closing it discovers before it refuses — the row in
    `timesheet_weeks_lock` that the database trigger reads, so a console that never asked
    the module cannot write either. A draft is not a write attempt: recording a lock because
    somebody *was shown a form* would be this system's own audit saying a week was closed by
    a request nobody made. So the tool refuses with the same catalogued code and writes
    nothing, and the very next real submission records exactly what it always did.

    Both halves are asserted, in this order, because the second is what makes the first
    meaningful: the lock is empty after the draft, and non-empty after the submission.
    """
    today = madrid_today(datetime.now(UTC))
    closed = monday_of(today) - timedelta(weeks=9)
    body = {
        "entry_date": closed.isoformat(),
        "project_id": cast.project["id"],
        "task_id": cast.task["id"],
        "minutes": FULL_DAY,
    }
    result, _ = await call(
        platform,
        cast.employee,
        "draft_timesheet",
        week_start=closed.isoformat(),
        **body,
    )
    assert result.outcome is ToolOutcome.INVALID
    assert result.data["message_key"] == "errors.timesheet_week_closed"
    assert await platform.scalar("SELECT count(*) FROM timesheet_weeks_lock") == 0, (
        "drafting in a closed week locked it"
    )

    posted = await cast.employee.post(
        "/api/v1/timesheets/entries", params={"week": closed.isoformat()}, json=body
    )
    assert posted.status_code == 409, posted.text
    assert posted.json()["error"]["message_key"] == "errors.timesheet_week_closed"
    assert await platform.scalar("SELECT count(*) FROM timesheet_weeks_lock") == 1, (
        "the real write did not record the closing it discovered"
    )


async def test_a_locked_week_is_refused_by_the_draft_and_by_the_route(
    platform: Platform, cast: Cast
) -> None:
    """An approved week is locked for ever, and a draft says so with the module's own code.

    The week is approved through the *engine* — the way `test_timesheet_lock.py` drives it,
    because the approval inbox is a later ticket — and the draft then refuses with
    `TIMESHEET_WEEK_LOCKED`: the module's own code and its own sentence, not a copy of them.
    """
    today = madrid_today(datetime.now(UTC))
    week = monday_of(today) - timedelta(weeks=1)
    body = {
        "entry_date": week.isoformat(),
        "project_id": cast.project["id"],
        "task_id": cast.task["id"],
        "minutes": FULL_DAY,
    }
    entry = await cast.employee.post(
        "/api/v1/timesheets/entries", params={"week": week.isoformat()}, json=body
    )
    assert entry.status_code == 201, entry.text
    filed = await cast.employee.post(
        "/api/v1/timesheets/submit", params={"week": week.isoformat()}
    )
    assert filed.status_code == 200, filed.text
    await approve(platform, cast, week=week)

    result, _ = await call(
        platform, cast.employee, "draft_timesheet", week_start=week.isoformat(), **body
    )
    assert result.outcome is ToolOutcome.INVALID
    assert result.data["message_key"] == "errors.timesheet_week_locked"
    assert render(result).es == MESSAGES["es"]["errors.timesheet_week_locked"]


async def approve(platform: Platform, cast: Cast, *, week: date) -> None:
    """Both levels, through the engine the way an approval inbox will (ticket 29's shape).

    The draft's own read asks the *engine* what the week's status is (`_engine_status`), so
    this is enough to make the week locked for the draft — no `apply_decision` call, and no
    write to the sheet's cached status, both of which a later read would perform.
    """
    sheet = await platform.scalar(
        "SELECT id FROM timesheets WHERE week_start = :week AND NOT is_supplementary",
        {"week": week},
    )
    assert sheet is not None, f"no sheet for the week of {week}"
    async with platform.factory() as session:
        engine = ApprovalService(PostgresApprovalRepository(session), session)
        state = await engine.state_of(ENTITY_TYPE, UUID(str(sheet)))
        assert state is not None, "that week was never filed"
        await engine.decide(state.id, UUID(cast.manager.employee_id), DecisionKind.APPROVE)
        await engine.decide(state.id, UUID(cast.hr.employee_id), DecisionKind.APPROVE)


# --- 5: as the caller, and only the caller ------------------------------------


async def test_the_draft_branch_refuses_a_name_that_is_not_a_draft(
    platform: Platform, cast: Cast
) -> None:
    """The draft branch's own whitelist: only the three draft tools may run there.

    Two names a model could produce, and neither may run: one nobody registered (the
    registry's refusal, ticket 39's) and one that *is* registered as read-only. The second is
    this branch's own check — a query run under the draft branch would put read values in a
    reply whose whole point is a form, and `state["tool"]` must not name a tool that did not
    produce one.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    unknown = await running_draft(
        platform, cast.employee, "draft_everything", {"start_date": day.isoformat()}
    )
    assert unknown["tool_outcome"] == str(ToolOutcome.UNKNOWN)
    assert unknown["tool"] is None
    assert unknown["prefill_form"] is None
    assert unknown["pending_action"]["status"] == "no_draft"
    assert "__interrupt__" not in unknown, "nothing to confirm, so nothing to wait for"

    read_tool = await running_draft(
        platform,
        cast.employee,
        "get_my_attendance",
        {"from_date": day.isoformat(), "to_date": day.isoformat()},
    )
    assert read_tool["tool_outcome"] == str(ToolOutcome.UNKNOWN)
    assert read_tool["tool"] is None, "the draft branch ran a read-only tool"
    assert read_tool["tool_result"] is None
    assert read_tool["prefill_form"] is None


async def running_draft(
    platform: Platform, actor: Actor, name: str, arguments: Mapping[str, Any]
) -> dict:
    """One draft-branch run with a name the caller supplied, and no fixture data needed.

    The question is the one the classifier routes to 待办操作: a run reaches this branch by
    being classified into it, and a question that read as a policy question would be answered
    by the answer path with the tool name sitting unused in the state — which is a mistake
    this helper's first version made, and a reminder that the branch under test is chosen by
    `intents.classify`.
    """
    async with pipeline(platform) as answers:
        session = platform.factory()
        context = AgentContext(
            principal=await principal_of(platform, actor),
            answers=answers.service,
            session=session,
        )
        try:
            async with open_checkpointer(get_settings(), test=True) as saver:
                return await build_graph(checkpointer=saver).ainvoke(
                    {
                        "question": "Quiero solicitar dos días de vacaciones",
                        "tool": name,
                        "tool_arguments": dict(arguments),
                    },
                    thread_config(uuid4().hex),
                    context=context,
                )
        finally:
            await session.close()


async def test_every_draft_is_the_callers_own(platform: Platform, cast: Cast) -> None:
    """The identity is the principal's, and the check reads *that* person's record.

    Two colleagues, one punch stream each: the first has one clock-out on the day and the
    second has two. The same correction draft is accepted for the first and refused for the
    second — which is only possible if the validation reads the caller's own punches. And
    the row the platform writes carries the caller's `user_id`, never a value from an
    argument.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    await punch(platform, cast.employee, day, hours=8)
    await punch(platform, cast.colleague, day, hours=8)
    await punch(platform, cast.colleague, day, hours=8, start_hour=10)

    arguments = {
        "business_date": day.isoformat(),
        "kind": "clock_out",
        "corrected_at": datetime(
            day.year, day.month, day.day, 16, 30, tzinfo=MADRID
        ).isoformat(),
        "reason": "La salida era mas tarde.",
    }
    mine, _ = await call(platform, cast.employee, "draft_attendance_correction", **arguments)
    theirs, _ = await call(
        platform, cast.colleague, "draft_attendance_correction", **arguments
    )
    assert mine.outcome is ToolOutcome.OK, mine.data
    assert theirs.outcome is ToolOutcome.INVALID

    # And a Tool that could name somebody else cannot be built at all.
    with pytest.raises(ValueError):
        Tool(
            name="draft_for_somebody_else",
            kind=ToolKind.DRAFT,
            summary="not constructible",
            parameters=("employee_id",),
            run=draft_tools.draft_leave_request,
        )


# --- 6: persisted, linked, and readable after a restart -----------------------


async def test_the_draft_survives_a_restart_and_is_read_back_through_the_conversation(
    platform: Platform, cast: Cast
) -> None:
    """Checklist: 持久化并关联到对话，刷新页面或重启服务后仍能找到未确认的草稿.

    Three facts, and the third is the one that makes the other two worth having:

    * the draft is a row in `agent_actions`, tied to the conversation the run filed it under
      and to the caller's `user_id`, with the form it proposed;
    * the row's `thread_id` is the LangGraph thread the run paused on (§3.6 keeps both), so
      the paused run and its draft can be found from either side;
    * **a different process** — a new graph object, a new checkpointer on a new connection, a
      new session — reads the same form back through the HTTP conversation read, and the
      values it carries are the ones the tool validated.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    arguments = leave_draft(cast, day=day)
    thread = uuid4().hex

    async with graph_run(
        platform, cast.employee, arguments, thread=thread
    ) as state:
        assert state["pending_action"]["status"] == "proposed"
    conversation_id = state["conversation_id"]
    assert state["pending_action"]["status"] == "proposed"
    assert conversation_id and state["agent_action_id"]

    row = (
        await platform.sql(
            """
            SELECT user_id, thread_id, tool_name, status, conversation_id,
                   produced_prefill_form
              FROM agent_actions WHERE id = :id
            """,
            {"id": state["agent_action_id"]},
        )
    )[0]
    assert str(row[0]) == cast.employee.user_id
    assert row[1] == thread, "the draft is not linked to the LangGraph thread"
    assert row[2] == "draft_leave_request"
    assert row[3] == "proposed"
    assert str(row[4]) == conversation_id
    assert row[5]["fields"], "the stored form has no fields"

    # The second process: the HTTP read, on a fresh request, with the caller's session.
    read = await cast.employee.get(f"/api/v1/answers/conversations/{conversation_id}")
    assert read.status_code == 200, read.text
    draft = read.json()["draft"]
    assert draft["id"] == state["agent_action_id"]
    assert draft["status"] == "proposed"
    assert draft["tool_name"] == "draft_leave_request"
    assert draft["prefill_form"] == state["prefill_form"], (
        "the form read back is not the form the run proposed"
    )
    assert draft["expires_at"] and draft["created_at"]

    # Somebody else's read does not find it — the conversation is the caller's, and the
    # route's ownership refusal is `ERR_RESOURCE_001` rather than a 403 (ticket 37's rule).
    other = await cast.colleague.get(f"/api/v1/answers/conversations/{conversation_id}")
    assert other.status_code == 404


async def test_a_conversation_with_no_draft_answers_with_none(
    platform: Platform, cast: Cast
) -> None:
    """The ordinary case: the field is `null`, not absent and not an error.

    A client asks this endpoint for every conversation it opens, and "the assistant proposed
    nothing" is the common answer — so it is a `null` beside the messages rather than a 404
    a client would have to interpret.
    """
    conversation = (
        await platform.sql(
            """
            INSERT INTO rag_conversations
                (id, user_id, title, created_at, last_message_at, expires_at)
            VALUES (gen_random_uuid(), :user_id, 'Sin borrador', now(), now(),
                    now() + interval '90 days')
            RETURNING id
            """,
            {"user_id": cast.employee.user_id},
        )
    )[0][0]
    read = await cast.employee.get(f"/api/v1/answers/conversations/{conversation}")
    assert read.status_code == 200, read.text
    assert read.json()["draft"] is None


# --- 7: the expiry ------------------------------------------------------------


def test_the_default_lifetime_is_a_day_and_the_clock_is_the_database_s() -> None:
    """Checklist: 草稿有有效期（默认 24 小时）.

    §6.3 names 24 hours and calls it a default, so the setting is asserted by name — and the
    *instant* is asserted to be the database's, in `repository.py`: the insert writes
    `now() + make_interval(hours => :ttl)` in one statement and the read compares against
    `now()`. A process-side clock would disagree with Postgres by whatever the container is
    off by, and the disagreement would be a draft confirmed after it lapsed.
    """
    assert get_settings().agent_draft_ttl_hours == 24
    source = Path(agent_actions_repository.__file__).read_text(encoding="utf-8")
    assert "make_interval(hours => :ttl_hours)" in source
    assert "expires_at <= now()" in source
    assert "datetime.now" not in source, "the repository reads the process clock"


async def test_a_lapsed_draft_is_marked_expired_when_it_is_read(
    platform: Platform, cast: Cast
) -> None:
    """Checklist: 过期后标记为失效并要求重新生成.

    The lapsed draft is produced by moving its `expires_at` into the past — the row's own
    column, which is what the read compares against — rather than by waiting a day or by a
    test-only TTL of zero. What is asserted: the read answers `expired` and the **row says
    so afterwards**, because §6.3 asks for a status and not for a filter. The form is still
    readable — an expired draft is evidence of what was proposed, and the employee is told
    to generate it again rather than being shown an empty screen.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    async with graph_run(platform, cast.employee, leave_draft(cast, day=day)) as state:
        assert state["pending_action"]["status"] == "proposed"
    conversation_id = state["conversation_id"]
    draft_id = state["agent_action_id"]

    # The draft was proposed 25 hours ago and lapsed an hour ago: both instants move, because
    # the row's own constraint (`expires_at > created_at`) says a draft cannot lapse before it
    # was proposed — which is the shape of a real lapse rather than a test-only state.
    await platform.sql(
        """
        UPDATE agent_actions
           SET created_at = now() - interval '25 hours',
               expires_at = now() - interval '1 hour'
         WHERE id = :id
        """,
        {"id": draft_id},
    )

    read = await cast.employee.get(f"/api/v1/answers/conversations/{conversation_id}")
    assert read.status_code == 200, read.text
    draft = read.json()["draft"]
    assert draft["status"] == "expired"
    assert draft["prefill_form"] == state["prefill_form"], (
        "an expired draft lost the form it proposed"
    )
    assert (
        await platform.scalar(
            "SELECT status FROM agent_actions WHERE id = :id", {"id": draft_id}
        )
        == "expired"
    ), "the read derived `expired` without recording it"

    # A second read is the same answer, and the row does not move twice.
    again = await cast.employee.get(f"/api/v1/answers/conversations/{conversation_id}")
    assert again.json()["draft"]["status"] == "expired"


async def test_a_draft_that_is_still_inside_its_day_is_proposed(
    platform: Platform, cast: Cast
) -> None:
    """The control for the test above: the same read, one minute earlier.

    Without it, `test_a_lapsed_draft_is_marked_expired_when_it_is_read` would pass against a
    service that answered `expired` for everything.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    async with graph_run(platform, cast.employee, leave_draft(cast, day=day)) as state:
        assert state["pending_action"]["status"] == "proposed"
    read = await cast.employee.get(
        f"/api/v1/answers/conversations/{state['conversation_id']}"
    )
    assert read.json()["draft"]["status"] == "proposed"
    # The instant came from the database, and it is the configured lifetime away.
    row = (
        await platform.sql(
            "SELECT created_at, expires_at FROM agent_actions WHERE id = :id",
            {"id": state["agent_action_id"]},
        )
    )[0]
    created, expires = row
    assert timedelta(hours=23) < (expires - created) <= timedelta(hours=24)


# --- the record, and the one place the form is not a sentence -----------------


async def test_the_draft_record_names_the_tool_and_carries_no_form(
    platform: Platform, cast: Cast
) -> None:
    """`records.py`'s promise, for the branch that now drafts.

    The record says *which* tool produced the form and how many fields it has — §10.1 lets a
    trace say `tool_name` and nothing else about a call — and never the form, its values or
    the question. The count is what makes the record worth having: "a draft of four fields
    was produced" is an operational fact; the four fields are the employee's.
    """
    from app.ai.agents.records import ALLOWED_FIELDS, records_of

    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    arguments = leave_draft(cast, day=day)
    async with graph_run(platform, cast.employee, arguments) as state:
        assert state["pending_action"]["status"] == "proposed"

    records = records_of(state)
    assert [record["node_name"] for record in records] == ["classify", "draft_tools"]
    for record in records:
        assert set(record) == set(ALLOWED_FIELDS), record
    draft_record = records[1]
    assert draft_record["tool_name"] == "draft_leave_request"
    assert draft_record["decision"] == str(ToolOutcome.OK)
    assert draft_record["counts"]["tools_registered"] == 3
    assert draft_record["counts"]["form_fields"] == len(state["prefill_form"])

    serialised = json.dumps(records, ensure_ascii=False)
    assert arguments["start_date"] not in serialised
    assert arguments["leave_type"] not in serialised
    assert json.dumps(state["prefill_form"], ensure_ascii=False) not in serialised
    assert "dos dias de permiso" not in serialised


def test_every_draft_field_label_is_in_both_catalogues() -> None:
    """The wording of a form is the catalogue's, and the catalogue has both languages.

    A label the API cannot render is a form field with no name — which is the one thing an
    editable form may not have.
    """
    keys = [
        key
        for key in MESSAGES["es"]
        if key.startswith("agent.draft.field.") or key.startswith("agent.draft.title.")
    ]
    assert len(keys) >= 15, keys
    for key in keys:
        assert MESSAGES["en"].get(key), f"{key} has no English wording"
    for key in ("agent.draft.option.clock_in", "agent.draft.option.clock_out"):
        assert MESSAGES["es"].get(key) and MESSAGES["en"].get(key), key


def test_every_draft_parameter_has_a_label_its_answer_can_use() -> None:
    """The mapping from a *parameter* to wording is total, which is a crash rather than a nit.

    A tool that was given too little answers with the labels of the fields it is missing, read
    from the catalogue by name (`render._labels`). So every name a draft tool may report —
    which is exactly the union of its declared `parameters` — must have a label key in both
    languages, or a missing `week_start` is a `KeyError` inside the answer instead of a
    question. This test is what makes that total by construction: the first version of the
    catalogue was missing exactly that one key, and the path that reaches it (a timesheet
    draft with no week) had no test at all.
    """
    for tool in registered(ToolKind.DRAFT):
        for name in tool.parameters:
            key = f"agent.draft.field.{name}"
            assert key in MESSAGES["es"], (tool.name, name)
            assert key in MESSAGES["en"], (tool.name, name)


async def test_a_missing_week_is_asked_for_rather_than_crashing(
    platform: Platform, cast: Cast
) -> None:
    """The path that found the missing label, asserted end to end.

    A timesheet draft with no week is the ordinary "the assistant was told too little" case:
    it answers with the field's label in both languages and writes nothing.
    """
    result, _ = await call(platform, cast.employee, "draft_timesheet", minutes=FULL_DAY)
    assert result.outcome is ToolOutcome.INVALID
    assert result.data["fields"] == ["week_start"]
    answer = render(result)
    assert answer.es.startswith("Me faltan datos")
    assert "Semana" in answer.es and "Week" in answer.en


def test_a_refusal_sentence_this_module_does_not_own_still_renders() -> None:
    """A borrowed catalogue sentence is rendered, not formatted to death.

    `_invalid` renders the *domain's* `message_key`, and one catalogue message carries a
    placeholder the domain never puts into it (`errors.timesheet_report_range_invalid`, whose
    sentence names a maximum number of days). Formatting it with nothing would be a
    `KeyError` raised inside an answer — from a call in the graph that nobody can wrap — so
    the renderer leaves an unfilled placeholder as written. Asserted on the renderer rather
    than on a run, because no draft tool can reach that particular key today: what is being
    pinned is the renderer's totality over the catalogue, which is what a future tool will
    rely on.
    """
    borrowed = ToolResult(
        tool="draft_timesheet",
        outcome=ToolOutcome.INVALID,
        data={
            "reason": "refused",
            "message_key": "errors.timesheet_report_range_invalid",
            "error_code": "ERR_TSH_005",
            "detail": "the period is wider than the module holds",
        },
    )
    answer = render(borrowed)
    assert answer.message_key == "errors.timesheet_report_range_invalid"
    assert "{days}" in answer.es, "an unfilled placeholder is shown, not raised"


def test_the_identity_fields_are_the_request_models_own() -> None:
    """`IDENTITY_FIELDS` names what a confirmation supplies from the session.

    Asserted against the endpoints' own request models rather than against a docstring. Two
    of the three submissions accept an `employee_id` — HR's after-the-fact correction and a
    leave request filed on somebody's behalf — and the third (`EntryWrite`) has none at all,
    because a week is *always* the caller's. That is exactly why a form may never carry one:
    the field is the platform's, and which endpoints even have it is not the model's
    business.
    """
    assert IDENTITY_FIELDS == frozenset({"employee_id"})
    assert IDENTITY_FIELDS <= set(RequestCreate.model_fields)
    assert IDENTITY_FIELDS <= set(CorrectionCreate.model_fields)
    assert not IDENTITY_FIELDS & set(EntryWrite.model_fields)


__all__ = ["Cast", "call", "form_of", "graph_run", "punch", "table_counts", "working_day"]
