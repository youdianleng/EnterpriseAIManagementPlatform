"""Ticket 39: DESIGN §6.2's read-only tools, executed **as the caller**.

Nine checklist lines, and the test that pins each:

* 提供工具：查我的考勤 / 年假余额 / 工时表状态 / 同事联系方式 —
  `test_my_attendance_states_the_figures_the_query_returned`,
  `test_my_leave_balance_states_the_services_own_remaining_days`,
  `test_my_timesheets_counts_the_callers_weeks`,
  `test_a_contact_is_the_directory_projection_and_nothing_beyond_it`
* 提供工具：经理查直属下属的考勤或工时汇总 —
  `test_the_team_summary_reaches_the_callers_direct_reports_and_nobody_else`
* 所有只读工具以被调用者的身份执行，复用权限内核 —
  `test_every_self_tool_reads_the_callers_own_record`,
  `test_every_tool_asks_the_permission_kernel_for_its_own_action`
* 经理范围严格限定直属下属，非下属返回空且不提示"存在但无权" —
  `test_a_stranger_is_absent_and_indistinguishable_from_an_empty_team`
* 通讯录可见性规则，不返回住址、编号 —
  `test_an_email_outside_the_callers_departments_is_absent_not_denied`
* 工具返回结构化数据，回答中的数字直接来自查询结果 —
  `test_the_answer_states_the_tool_s_own_figure_and_the_sql_s_own_figure`
* 失败时回退为"无法获取该数据"，不编造数值 —
  `test_a_failing_tool_states_no_figure_at_all`
* 普通员工调用经理工具被拒；经理查询非下属返回空 —
  `test_an_ordinary_employee_is_refused_the_team_tool`,
  `test_a_stranger_is_absent_and_indistinguishable_from_an_empty_team`
* 工具清单是白名单，未注册的工具无法被模型调用 —
  `test_an_unregistered_name_is_refused_and_nothing_runs`

Plus the constraint this ticket is the first half of: **no registered tool can write** —
`test_no_registered_tool_can_reach_a_write`, which walks every implementation's source.

**Real infrastructure, and the repository's own seams only.** PostgreSQL is real, Redis is
real, and the punches and weeks below are real rows written through the API (timesheets) or
into the punch stream the way `test_attendance_events.py` writes them (attendance). The one
double is `StreamedChatModel`, ticket 34's offline development adapter, and it is here to
make one claim: a data question **does not call a model at all**.

**What is asserted, and why it is the value rather than the call.** A test that checked "a
tool ran" would pass against an assistant that answered 3 days because a model inferred it,
which is the failure this ticket exists to prevent. So the figures below are compared
against the *test's own* data — the minutes it inserted, the balance it granted, the rows
`timesheets` holds — and the answer's text is asserted to contain exactly those numbers.
The failure case is the other half: the answer must contain **no digit at all**.
"""

import ast
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.ai.agents import (
    AgentContext,
    Intent,
    build_graph,
    open_checkpointer,
    thread_config,
)
from app.ai.agents.records import ALLOWED_FIELDS, records_of
from app.ai.tools import (
    ALLOWED_PARAMETERS,
    REGISTRY,
    Tool,
    ToolCall,
    ToolContext,
    ToolKind,
    ToolOutcome,
    ToolResult,
    UnknownTool,
    arguments_for,
    invoke,
    lookup,
    readonly,
    registered,
    render,
    select_tool,
)
from app.ai.tools import services as tool_services
from app.ai.tools.render import rendered_tools
from app.config import get_settings
from app.core.messages import MESSAGES
from app.domain.access.kernel import Action, can
from app.domain.attendance.business_day import madrid_today
from app.domain.attendance.models import EventSource, EventType
from app.domain.leave.service import LeaveService
from app.domain.timesheet.models import monday_of
from app.domain.timesheet.service import TimesheetService
from app.repositories.employee import PostgresEmployeeRepository
from tests.support.platform import Actor, Platform
from tests.test_agent_graph import pipeline, principal_of

#: The keys every data question must be answerable from: the tool that ran, its structured
#: values, the sentence they were rendered into, and how the call ended.
RESULT_KEYS = ("tool", "tool_result", "tool_answer", "tool_outcome")

#: An attendance day that is over: in at nine, out at five. Eight hours, 480 minutes, and
#: a pair a derivation cannot mistake for a still-open shift.
FULL_DAY = 480
LAST_WEEK = timedelta(days=7)

#: The words a sentence must not use when a caller asked about somebody outside their
#: reach. 「不得提示"存在但无权"」 is a statement about wording as well as about rows: an answer
#: that said "not permitted" would tell the reader the data exists.
ORACLE_WORDS = ("permission", "permiso", "forbidden", "denied", "no tienes", "withheld")


# --- the cast -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cast:
    """The people the reaches are told apart by.

    `manager` and `employee` share a department, and so do `manager` and `colleague`; the
    difference between the last two is that one reports to `manager` and the other does
    not. That is the whole of the ticket's manager rule, so the cast is built to make the
    department clause the *wrong* answer.
    """

    employee: Actor
    colleague: Actor
    manager: Actor
    other_manager: Actor
    #: A managerial position whose reports set is empty: the control the "nothing to show"
    #: answer is compared against.
    lonely_manager: Actor
    outsider: Actor
    hr: Actor
    department_a: str
    department_b: str


@pytest.fixture
async def cast(platform: Platform) -> Cast:
    suffix = uuid4().hex[:8]
    department_a = await platform.department(f"roA{suffix}")
    department_b = await platform.department(f"roB{suffix}")

    # **Nobody here is another manager's approver.** `reports_employee_ids` is built from
    # the assignments that *name* somebody, so an approver relationship is a reports
    # relationship — and a cast in which the two managers approve for each other would give
    # each of them the other as a report, which is exactly the confusion these tests exist
    # to catch. Managers therefore name no approver, and every employee names one.
    manager = await platform.account(roles=("employee",))
    other_manager = await platform.account(roles=("employee",))
    lonely_manager = await platform.account(roles=("employee",))
    await platform.assign(
        manager.employee_id,
        department_a,
        await platform.position(department_a, f"jefeA{suffix}", is_managerial=True),
    )
    await platform.assign(
        other_manager.employee_id,
        department_b,
        await platform.position(department_b, f"jefeB{suffix}", is_managerial=True),
    )
    await platform.assign(
        lonely_manager.employee_id,
        department_a,
        await platform.position(department_a, f"solo{suffix}", is_managerial=True),
    )

    position = await platform.position(department_a, f"tecA{suffix}")
    # In the manager's own department and reporting to somebody else: the reach that must
    # not be reached.
    colleague = await platform.account(roles=("employee",))
    await platform.assign(
        colleague.employee_id,
        department_a,
        position,
        manager_employee_id=other_manager.employee_id,
    )
    employee = await platform.account(roles=("employee",))
    await platform.assign(
        employee.employee_id,
        department_a,
        position,
        manager_employee_id=manager.employee_id,
    )
    # The other side of the department boundary, for the withheld-email assertion.
    outsider = await platform.account(roles=("employee",))
    await platform.assign(
        outsider.employee_id,
        department_b,
        await platform.position(department_b, f"tecB{suffix}"),
        manager_employee_id=other_manager.employee_id,
    )

    for actor, first, last in ((colleague, "Ana", "Martín"), (outsider, "Beto", "Ruiz")):
        await platform.sql(
            "UPDATE employees SET first_name = :first, last_name = :last WHERE id = :id",
            {"first": first, "last": last, "id": actor.employee_id},
        )

    return Cast(
        employee=employee,
        colleague=colleague,
        manager=manager,
        other_manager=other_manager,
        lonely_manager=lonely_manager,
        outsider=outsider,
        hr=await platform.account(roles=("hr",)),
        department_a=department_a,
        department_b=department_b,
    )


# --- fixtures the tools read --------------------------------------------------


async def punch(platform: Platform, actor: Actor, day: date, *, hours: int = 8) -> None:
    """A closed shift in the real punch stream: in at nine, out `hours` later.

    Written with SQL rather than through `POST /attendance/clock`, because the endpoint
    punches *now* and these tests need a day that is over. The rows are the table's own —
    `test_attendance_events.py` inserts them the same way for the same reason — and the
    tool derives the day from them exactly as it derives a day somebody clocked.
    """
    from zoneinfo import ZoneInfo

    madrid = ZoneInfo("Europe/Madrid")
    for kind, hour in ((EventType.CLOCK_IN, 9), (EventType.CLOCK_OUT, 9 + hours)):
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
                    day.year, day.month, day.day, hour, tzinfo=madrid
                ).astimezone(UTC),
                "day": day,
                "source": str(EventSource.WEB),
            },
        )


def period(today: date, *, days: int = 2) -> dict[str, str]:
    """A closed range of whole days before today, as a tool's arguments.

    Explicit rather than defaulted, and that is deliberate: a test that relied on "this
    month" would fail on the first of a month for a reason that has nothing to do with
    tools. The default period is asserted separately, on `arguments_for` alone.
    """
    first = today - timedelta(days=days)
    last = today - timedelta(days=1)
    return {"from_date": first.isoformat(), "to_date": last.isoformat()}


async def make_project(actor: Actor, *, department_id: str) -> dict:
    created = await actor.post(
        "/api/v1/projects",
        json={
            "code": f"ro{uuid4().hex[:6]}",
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


async def make_task(actor: Actor, project_id: str) -> dict:
    created = await actor.post(
        f"/api/v1/projects/{project_id}/tasks",
        json={"code": f"t{uuid4().hex[:6]}", "name_es": "Tarea", "name_en": "Task"},
    )
    assert created.status_code == 201, created.text
    return created.json()


async def book(actor: Actor, *, week: date, project_id: str, task_id: str) -> None:
    """One entry, which creates the week as a draft. Nothing is filed."""
    response = await actor.post(
        "/api/v1/timesheets/entries",
        params={"week": week.isoformat()},
        json={
            "entry_date": week.isoformat(),
            "project_id": project_id,
            "task_id": task_id,
            "minutes": FULL_DAY,
        },
    )
    assert response.status_code == 201, response.text


# --- driving the graph --------------------------------------------------------


@dataclass
class Running:
    """One compiled graph, one context, and the model it must not call."""

    graph: Any
    context: AgentContext
    model: Any

    async def run(
        self,
        question: str,
        *,
        tool: str | None = None,
        arguments: dict[str, Any] | None = None,
        today: date | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"question": question}
        if tool is not None:
            payload["tool"] = tool
        if arguments is not None:
            payload["tool_arguments"] = dict(arguments)
        if today is not None:
            raise AssertionError("pass `today` to `read_only`, not to `run`")
        return await self.graph.ainvoke(
            payload,
            {"configurable": {"thread_id": uuid4().hex}},
            context=self.context,
        )

    @property
    def calls(self) -> list:
        return self.model.calls


@asynccontextmanager
async def read_only(
    platform: Platform, actor: Actor, *, today: date | None = None
) -> AsyncIterator[Running]:
    """The graph on this test's database, with the caller's real principal and a session.

    The session is what the read-only tools read through (`AgentContext.session`), and it
    is closed here for the reason `test_agent_graph.pipeline` documents: a retrieval or a
    read that leaves a transaction open makes the next test's `TRUNCATE` wait for it.
    """
    principal = await principal_of(platform, actor)
    async with pipeline(platform) as answers:
        session = platform.factory()
        context = AgentContext(
            principal=principal,
            answers=answers.service,
            session=session,
            today=today or madrid_today(datetime.now(UTC)),
        )
        try:
            yield Running(
                graph=build_graph(), context=context, model=answers.model
            )
        finally:
            await session.close()


def question_for(intent: Intent) -> str:
    """One question per intent, and the read-only one is the ticket's own example."""
    return {
        Intent.READ_ONLY_QUERY: "¿Cuántos días de vacaciones me quedan?",
        Intent.POLICY_QUESTION: "¿Cuántos días de permiso por matrimonio corresponden?",
    }[intent]


# --- 9: the whitelist ---------------------------------------------------------


def test_the_registry_is_design_6_2s_read_only_rows_and_nothing_else() -> None:
    """Checklist: 工具清单是白名单. Plus constraint B's first layer, as a value.

    Five names, each mapping to a `Tool` whose own name is the key it is filed under, and
    a `ToolKind` with no `WRITE` member at all — so "the agent has no write tool" is a
    fact about the type rather than a rule somebody has to keep.

    **The draft half is ticket 40's and this assertion is now about the read half.** It
    used to say `registered(ToolKind.DRAFT) == ()`; ticket 40 registered §6.2's three draft
    rows, so the set this file owns is `registered(ToolKind.READ_ONLY)`, and what it still
    asserts about the whole registry is the part that is about *every* tool: the name it is
    filed under, and the kind.
    """
    assert {tool.name for tool in registered(ToolKind.READ_ONLY)} == {
        "get_my_attendance",
        "get_my_leave_balance",
        "get_my_timesheets",
        "get_colleague_contact",
        "get_team_attendance_summary",
    }
    assert not hasattr(ToolKind, "WRITE"), "a write kind exists"
    for name, tool in REGISTRY.items():
        assert tool.name == name, f"{name} is filed under another tool's name"
        assert tool.kind in {ToolKind.READ_ONLY, ToolKind.DRAFT}


def test_design_6_2s_search_policy_is_not_a_second_retrieval_path() -> None:
    """`search_policy` is deliberately absent, and the graph's own branch is why.

    §6.2 lists it; the ticket's wording is 「如果把它作为工具暴露」 — and exposing it would
    be a second entry point to ticket 35's filtered retrieval, returning prose where every
    other tool returns values. 制度问答 already reaches it through `answer_policy`.
    """
    assert "search_policy" not in REGISTRY
    assert "search_policy" not in {tool.name for tool in registered()}


def test_an_unregistered_name_is_refused_and_nothing_runs() -> None:
    """Checklist: 未注册的工具无法被模型调用.

    Three layers of the same claim: `lookup` raises for a name nobody registered; the
    selector can only ever produce a name that *is* registered, asserted against the
    registry itself; and a run that names a tool directly gets the "no registered tool"
    answer with no result at all — so nothing was read.
    """
    with pytest.raises(UnknownTool):
        lookup("get_everybodys_attendance")
    with pytest.raises(UnknownTool):
        lookup("get_my_attendance ")  # a trailing space is a different name

    # Every name the lexical selector can produce is in the registry. A selector that
    # named a tool that does not exist would be a hole in the whitelist.
    for name, _cues in __import__(
        "app.ai.tools.selection", fromlist=["SELECTORS"]
    ).SELECTORS:
        assert name in REGISTRY


async def test_a_named_tool_that_is_not_registered_is_answered_without_a_result(
    platform: Platform, cast: Cast
) -> None:
    """The graph-level half: a name the model produced that nobody registered."""
    async with read_only(platform, cast.employee) as running:
        state = await running.run(
            question_for(Intent.READ_ONLY_QUERY),
            tool="delete_all_attendance",
            arguments={"from_date": "2026-01-01", "to_date": "2026-01-31"},
        )

    assert state["tool_outcome"] == str(ToolOutcome.UNKNOWN)
    assert state["tool_result"] is None
    assert state["tool_answer"]["message_key"] == "agent.tool.unknown"
    # Nothing ran, so nothing is named — and the name the model invented is not carried
    # into the state's `tool` or into the record's `tool_name`, which is a registry key by
    # contract (§10.1: a trace field must not be a place model output can be written to).
    assert state["tool"] is None
    assert state["tool_arguments"] == {}
    assert records_of(state)[1]["tool_name"] is None
    assert "delete_all_attendance" not in json.dumps(records_of(state), ensure_ascii=False)
    assert running.calls == [], "an unregistered tool reached a model"
    assert no_digits(state["tool_answer"]["text"])


# --- constraint B: no registered tool can write -------------------------------


#: Calls that write. Every name is either a session operation, a repository write, or a
#: *service* method that commits — including `read_week` and `sheets_of`, which are reads
#: that persist the engine's answer (`TimesheetService.apply_decision`) and are therefore
#: writes as far as this assertion is concerned. `list_weeks` is absent on purpose: it is
#: the same module's read that writes nothing, and it is the one the timesheet tool uses.
WRITE_CALLS = frozenset(
    {
        "commit", "flush", "add", "add_all", "merge", "delete", "refresh",
        "execute", "executemany", "scalar", "scalars",
        "clock", "recompute_day", "rebuild_day", "append_event",
        "add_entry", "update_entry", "remove_entry", "copy_previous_week",
        "submit", "open_supplement", "apply_decision", "read_week", "sheets_of",
        "create_type", "update_type", "set_balance", "draft", "decide", "withdraw",
        "settle_decided", "settle",
        "create", "update", "save", "save_private", "save_assignment",
        "end_assignment", "set_primary", "save_type",
        "ingest", "reprocess", "lock_week", "apply",
    }
)

#: A string that *is* a write statement: the first word is a write verb. Anchored at the
#: start of a line so that prose in a docstring cannot trip it, and docstrings are skipped
#: anyway.
WRITE_SQL = re.compile(
    r"^\s*(insert|update|delete|truncate|alter|drop|grant|create)\b", re.IGNORECASE
)

TOOLS_PACKAGE = Path(readonly.__file__).parent


def write_calls(source: str) -> set[str]:
    """The write-shaped calls and SQL literals in one module's source.

    A function rather than a fixture, because the positive control below has to be able to
    feed it a source that *does* write: a walker that finds nothing because it looks for
    nothing would make the assertion about the real modules vacuous.
    """
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            name = (
                target.attr
                if isinstance(target, ast.Attribute)
                else target.id
                if isinstance(target, ast.Name)
                else None
            )
            if name in WRITE_CALLS:
                found.add(name)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            if WRITE_SQL.search(node.value):
                found.add(f"sql:{node.value.split()[0].lower()}")
    return found


def test_the_write_walker_catches_a_module_that_writes() -> None:
    """The positive control. Without it, `write_calls` returning `set()` proves nothing."""
    assert write_calls(
        "async def f(session, row):\n"
        "    session.add(row)\n"
        "    await session.commit()\n"
    ) == {"add", "commit"}
    assert write_calls('STATEMENT = "UPDATE employees SET salary = 1"') == {"sql:update"}
    assert write_calls('STATEMENT = """\nDELETE FROM audit_log\n"""') == {"sql:delete"}
    # And the negative control: a read through a domain service is not flagged.
    assert write_calls(
        "async def f(service, who, start, end):\n"
        "    return await service.range_view(who, start, end)\n"
    ) == set()


def test_no_registered_tool_can_reach_a_write() -> None:
    """Constraint B's structural layer, for every tool this registry holds.

    `codebase-design.md` §6: 「测试断言 AI 可触达的工具集合中不存在写库工具」. The registry is
    that set, so this walks the source of the whole `app/ai/tools` package — the five
    implementations *and* the wiring that builds their domain services, plus ticket 40's
    three draft tools — and fails on a write-shaped call or a write statement.
    `tests/test_agent_draft_tools.py` is where the draft half's own behaviour is asserted;
    the walk is one function over one package, so it covers both by construction.
    """
    modules = sorted(TOOLS_PACKAGE.glob("*.py"))
    assert len(modules) >= 6, modules

    walked = {module.name for module in modules}
    for tool in registered():
        assert Path(tool.run.__code__.co_filename).name in walked, (
            f"{tool.name} is implemented outside the walked package: "
            f"{tool.run.__code__.co_filename}"
        )

    offenders = {
        module.name: found
        for module in modules
        if (found := write_calls(module.read_text(encoding="utf-8")))
    }
    assert offenders == {}, f"a tool module reaches a write: {offenders}"

    # Not vacuous in the other direction either: the package really does call methods, so
    # the walk above had something to look at.
    calls = sum(
        len(re.findall(r"\w+\(", module.read_text(encoding="utf-8")))
        for module in modules
    )
    assert calls > 50, calls


def test_no_tool_parameter_can_name_an_employee() -> None:
    """The checklist's non-negotiable half, as a closed vocabulary.

    「所有只读工具以被调用者的身份执行查询」 and 「不得给工具一个选择读谁的数据的参数」. A tool
    cannot even *declare* an employee id: `Tool.__post_init__` refuses a parameter outside
    `ALLOWED_PARAMETERS`, so an escalation would have to be an edit to that set — which the
    first assertion pins, so the edit is visible in a diff rather than in a prompt.

    Ticket 40 widened the literal with the draft tools' fields (a leave type, two dates, a
    punch kind and its instant, a week and a day, a project and a task, minutes, a note),
    and this test is why that widening is a *decision*: the assertion below is the list, so
    a name arriving in `ALLOWED_PARAMETERS` without a reader seeing it here is impossible.
    """
    assert ALLOWED_PARAMETERS == {
        # read-only (ticket 39)
        "from_date", "to_date", "year", "status", "name",
        # drafts (ticket 40)
        "leave_type", "start_date", "end_date", "attachment_reference",
        "business_date", "kind", "corrected_at", "reason",
        "week_start", "entry_date", "project_id", "task_id", "minutes", "note",
    }
    declared: set[str] = set()
    for tool in registered():
        declared |= set(tool.parameters)
    assert declared <= ALLOWED_PARAMETERS
    assert not {name for name in declared if "employee" in name or "user" in name}

    with pytest.raises(ValueError):
        Tool(
            name="get_someone_elses_attendance",
            kind=ToolKind.READ_ONLY,
            summary="not constructible",
            parameters=("employee_id",),
            run=readonly.my_attendance,
        )


# --- 3 & 6: as the caller, and from the query ---------------------------------


async def test_every_self_tool_reads_the_callers_own_record(
    platform: Platform, cast: Cast
) -> None:
    """Checklist: 所有只读工具以**被调用者的身份**执行查询.

    The two people are in the same department, report to the same manager and have
    different figures. Every self tool is run as `employee`, and every figure is the
    employee's own — the colleague's numbers appear nowhere. Then the same tools are run
    as the colleague and state *their* figures instead, which is what makes the first half
    an assertion about identity rather than about arithmetic.
    """
    today = madrid_today(datetime.now(UTC))
    window = period(today)
    first, last = date.fromisoformat(window["from_date"]), date.fromisoformat(window["to_date"])
    days = (last - first).days + 1
    for offset in range(days):
        await punch(platform, cast.employee, first + timedelta(days=offset), hours=8)
        await punch(platform, cast.colleague, first + timedelta(days=offset), hours=4)

    for actor, mine, theirs in (
        (cast.employee, 8 * 60 * days, 4 * 60 * days),
        (cast.colleague, 4 * 60 * days, 8 * 60 * days),
    ):
        async with read_only(platform, actor) as running:
            state = await running.run(
                "¿Cuántas horas he fichado?", tool="get_my_attendance", arguments=window
            )
        assert state["tool_result"]["worked_minutes"] == mine
        assert str(mine) in state["tool_answer"]["text"]
        assert str(theirs) not in state["tool_answer"]["text"]


async def test_my_attendance_states_the_figures_the_query_returned(
    platform: Platform, cast: Cast
) -> None:
    """Checklist: 回答中的数字直接来自查询结果，不允许模型重算或推测.

    Two closed days of eight hours each, written by the test. The figure in the sentence
    is compared with the number of minutes the test inserted — not with the tool's own
    field, which would be the tool agreeing with itself — and the tool's field is compared
    with the same number.
    """
    today = madrid_today(datetime.now(UTC))
    window = period(today)
    days = [today - timedelta(days=2), today - timedelta(days=1)]
    for day in days:
        await punch(platform, cast.employee, day, hours=8)

    async with read_only(platform, cast.employee) as running:
        state = await running.run(
            "¿Cuántas horas he fichado?", tool="get_my_attendance", arguments=window
        )

    expected = 8 * 60 * len(days)
    result = state["tool_result"]
    assert result["worked_minutes"] == expected, result
    assert result["worked_days"] == len(days)
    assert result["from_date"] == window["from_date"]
    assert result["to_date"] == window["to_date"]
    assert result["last_out"] is not None
    assert len(result["days"]) == len(days)
    for day, row in zip(days, result["days"], strict=True):
        assert row["business_date"] == day.isoformat()
        assert row["worked_minutes"] == 480

    answer = state["tool_answer"]
    assert str(expected) in answer["es"]
    assert str(expected) in answer["en"]
    assert answer["message_key"] == "agent.tool.my_attendance"
    assert state["tool"] == "get_my_attendance"
    assert state["tool_outcome"] == str(ToolOutcome.OK)
    assert running.calls == [], "a data question called a model"
    assert await platform.scalar("SELECT count(*) FROM rag_conversations") == 0


async def test_my_leave_balance_states_the_services_own_remaining_days(
    platform: Platform, cast: Cast
) -> None:
    """Checklist: 查我的年假余额 — and the figure is the ledger's, computed by the module.

    HR grants 22 + 5 in the year the tool defaults to. The answer's figure is compared
    against a SQL sum over `leave_balances` — the ledger the grant wrote — so a tool that
    had invented, rounded or recomputed the number would disagree with the row.
    """
    year = madrid_today(datetime.now(UTC)).year
    granted = await cast.hr.put(
        f"/api/v1/leave/balances/{cast.employee.employee_id}/{year}/annual",
        json={"entitled_days": 22, "carried_over_days": 5, "note": "convenio"},
    )
    assert granted.status_code == 200, granted.text

    async with read_only(platform, cast.employee) as running:
        state = await running.run(
            "¿Cuántos días de vacaciones me quedan?",
            tool="get_my_leave_balance",
            arguments={"year": year},
        )

    row = (
        await platform.sql(
            """
            SELECT entitled_days + carried_over_days - used_days - pending_days
              FROM leave_balances
             WHERE employee_id = :employee_id AND year = :year
            """,
            {"employee_id": cast.employee.employee_id, "year": year},
        )
    )[0]
    expected = int(row[0])
    assert expected == 27, "the fixture's grant is not the figure this test is about"

    annual = state["tool_result"]["annual"]
    assert [item["remaining_days"] for item in annual] == [expected]
    assert state["tool_result"]["year"] == year
    assert str(expected) in state["tool_answer"]["text"]
    assert state["tool_answer"]["message_key"] == "agent.tool.my_leave_balance"


async def test_my_timesheets_counts_the_callers_weeks(
    platform: Platform, cast: Cast
) -> None:
    """Checklist: 查我的工时表状态 — counts over the rows, and only the caller's rows.

    A draft week and a filed one for the employee, and a draft week for the colleague.
    The counts are compared with a SQL `GROUP BY status` over the same person's sheets, so
    the colleague's week has to be outside the query rather than subtracted afterwards.
    """
    current = monday_of(madrid_today(datetime.now(UTC)))
    project = await make_project(cast.manager, department_id=cast.department_a)
    task = await make_task(cast.manager, project["id"])

    await book(cast.employee, week=current, project_id=project["id"], task_id=task["id"])
    await book(
        cast.employee,
        week=current - timedelta(weeks=1),
        project_id=project["id"],
        task_id=task["id"],
    )
    filed = await cast.employee.post(
        "/api/v1/timesheets/submit",
        params={"week": (current - timedelta(weeks=1)).isoformat()},
    )
    assert filed.status_code == 200, filed.text
    await book(cast.colleague, week=current, project_id=project["id"], task_id=task["id"])

    rows = await platform.sql(
        """
        SELECT status, count(*) FROM timesheets
         WHERE employee_id = :employee_id AND NOT is_supplementary
         GROUP BY status
        """,
        {"employee_id": cast.employee.employee_id},
    )
    expected = {str(status): int(count) for status, count in rows}
    assert expected == {"draft": 1, "pending": 1}, expected

    async with read_only(platform, cast.employee) as running:
        state = await running.run(
            "¿Cuál es el estado de mi hoja de horas?", tool="get_my_timesheets"
        )

    result = state["tool_result"]
    assert {name: result["counts"][name] for name in expected} == expected
    assert set(result["counts"]) == {"draft", "pending", "approved", "rejected"}
    assert result["weeks"] == 2 and result["listed"] == 2
    assert result["filtered_by"] is None
    answer = state["tool_answer"]
    assert str(expected["draft"]) in answer["es"]
    assert str(expected["pending"]) in answer["es"]
    assert str(current) not in answer["text"], "a week leaked into the sentence"


async def test_a_period_the_caller_did_not_name_defaults_to_this_month() -> None:
    """The default argument, asserted without a database.

    `selection.arguments_for` is the seam ticket 42 replaces with a model's function call,
    so what it produces is worth pinning: this month, this year, all statuses. The
    attendance tests above name their period explicitly, which is why this is a unit test.
    """
    today = date(2026, 3, 15)
    assert arguments_for("get_my_attendance", "mis horas", today=today) == {
        "from_date": "2026-03-01",
        "to_date": "2026-03-31",
    }
    assert arguments_for("get_my_attendance", "ayer", today=today) == {
        "from_date": "2026-03-14",
        "to_date": "2026-03-14",
    }
    assert arguments_for("get_my_leave_balance", "mi saldo", today=today) == {"year": 2026}
    assert arguments_for("get_my_leave_balance", "el saldo de 2024", today=today) == {
        "year": 2024
    }
    assert arguments_for("get_my_timesheets", "mis horas", today=today) == {"status": ""}
    assert arguments_for("get_my_timesheets", "horas aprobadas", today=today) == {
        "status": "approved"
    }
    assert arguments_for("get_colleague_contact", "el email de nadie", today=today) is None


async def test_the_tools_call_the_domain_services_the_api_calls(
    platform: Platform, cast: Cast
) -> None:
    """「不要重新实现查询」: the wiring hands the tools the modules' own services.

    Asserted on the classes, because that is what "reuse" means at this layer: the
    attendance tool reads through `AttendanceService`, the leave tool through
    `LeaveService`, the timesheet tool through `TimesheetService` scoped to the caller, and
    the contact tool through the repository the directory endpoint lists with.
    """
    async with platform.factory() as session:
        assert isinstance(
            tool_services.attendance(session), tool_services.AttendanceService
        )
        assert isinstance(tool_services.leave(session), LeaveService)
        scoped = tool_services.timesheets(session, await principal_of(platform, cast.employee))
        assert isinstance(scoped, TimesheetService)
        assert scoped.employee_id == UUID(cast.employee.employee_id)
        assert isinstance(tool_services.directory(session), PostgresEmployeeRepository)


def test_every_tool_asks_the_permission_kernel_for_its_own_action() -> None:
    """「复用权限内核」 is a call, not a claim. Each tool names one catalogued action.

    Structurally: the module's source mentions each action, and the two self-only reaches
    really are self-only in the catalogue — so a tool cannot widen by accident without
    contradicting the kernel's own table.
    """
    source = Path(readonly.__file__).read_text(encoding="utf-8")
    for action in (
        "Action.ATTENDANCE_READ_OWN",
        "Action.LEAVE_READ_OWN",
        "Action.TIMESHEET_READ_OWN",
        "Action.EMPLOYEE_DIRECTORY",
        "Action.ATTENDANCE_READ_REPORT",
    ):
        assert action in source, action

    # The two reaches this ticket turns on, as the catalogue states them.
    assert can(with_employee({"manager"}), Action.ATTENDANCE_READ_REPORT).allowed is True
    assert can(with_employee(set()), Action.ATTENDANCE_READ_REPORT).allowed is False
    for action in (
        Action.ATTENDANCE_READ_OWN,
        Action.LEAVE_READ_OWN,
        Action.TIMESHEET_READ_OWN,
    ):
        assert action in __import__(
            "app.domain.access.permissions", fromlist=["SELF_ONLY_ACTIONS"]
        ).SELF_ONLY_ACTIONS


# --- 5: contacts are the projection -------------------------------------------


async def test_a_contact_is_the_directory_projection_and_nothing_beyond_it(
    platform: Platform, cast: Cast
) -> None:
    """Checklist: 查同事联系方式受通讯录可见性规则约束，不返回住址、编号等敏感字段.

    Two assertions make this "the same projection" rather than "a similar one": every row
    the tool returns is compared, field by field, with the row `GET /employees/directory`
    returns to the same caller for the same person; and the keys are a subset of
    `project_directory_row`'s own keys. Both lookups are done — a colleague, whose email
    the projection grants, and somebody in another department, whose email it withholds —
    because a rule that only ever grants is not exercised by the granting case. The
    sensitive fields are then asserted absent by name, because a test that compared shapes
    alone would not notice a *new* field.
    """
    directory = await cast.employee.get("/api/v1/employees/directory")
    assert directory.status_code == 200, directory.text
    listed = {row["employee_id"]: row for row in directory.json()}

    for name, actor in (("Ana Martín", cast.colleague), ("Beto Ruiz", cast.outsider)):
        async with read_only(platform, cast.employee) as running:
            state = await running.run(
                "¿Cuál es el email de mi equipo?",
                tool="get_colleague_contact",
                arguments={"name": name},
            )

        result = state["tool_result"]
        assert result["match_count"] == 1, result
        row = result["matches"][0]
        # Field by field against the endpoint's own row for the same person and the same
        # caller. The endpoint omits a field whose value is null
        # (`response_model_exclude_none`), so `.get` is what compares the two shapes
        # honestly: a null here and an omission there are the same statement, and any
        # field the tool invented would differ.
        endpoint = listed[actor.employee_id]
        assert set(row) <= set(endpoint) | {
            field for field, value in row.items() if value is None
        }
        for field, value in row.items():
            assert endpoint.get(field) == value, f"the directory says {field}={value!r}"
        assert set(row) <= set(readonly.CONTACT_FIELDS)

        # The projection's own keys, computed for this viewer — never a second rule.
        async with platform.factory() as session:
            from app.domain.access.snapshot import to_viewer_context
            from app.domain.employee.visibility import project_directory_row
            from app.repositories.employee import PostgresEmployeeRepository as Repo

            entries = await Repo(session).list_directory()
            entry = next(
                item for item in entries if str(item.employee_id) == actor.employee_id
            )
            projected = project_directory_row(
                to_viewer_context(await principal_of(platform, cast.employee)), entry
            )
        assert set(row) <= set(projected)

        serialised = json.dumps(result, ensure_ascii=False)
        for sensitive in (
            "address_line",
            "postal_code",
            "employee_no",
            "birth_date",
            "emergency_contact",
            "hire_date",
            "termination_date",
        ):
            assert sensitive not in serialised, f"{sensitive} left the projection"

    # The one field the two cases differ on, stated here so the loop above is not read as
    # "the projection grants everything": the endpoint itself withholds the outsider's
    # email, and the tool's row for them has no such key.
    assert listed[cast.colleague.employee_id]["email"] == cast.colleague.email
    assert "email" not in listed[cast.outsider.employee_id]


async def test_an_email_outside_the_callers_departments_is_absent_not_denied(
    platform: Platform, cast: Cast
) -> None:
    """The withheld half of the same rule: absent, and indistinguishable from unrecorded.

    `visibility.py` drops the key rather than nulling it so that a client cannot read
    "not allowed" as "not recorded". The answer must not put that distinction back: the
    sentence for a withheld email is the sentence for somebody with no email on record,
    and it says nothing about the email at all.
    """
    async with read_only(platform, cast.employee) as running:
        state = await running.run(
            "¿Cuál es el email de Beto Ruiz?",
            tool="get_colleague_contact",
            arguments={"name": "Beto Ruiz"},
        )

    result = state["tool_result"]
    assert result["match_count"] == 1
    assert "email" not in result["matches"][0], result
    assert result["email"] is None
    assert cast.outsider.email not in json.dumps(state["tool_result"], ensure_ascii=False)
    assert cast.outsider.email not in state["tool_answer"]["text"]
    # The row is still found and still names the person: what was withheld is one field.
    assert result["full_name"] == "Beto Ruiz"
    assert state["tool_answer"]["message_key"] == "agent.tool.colleague_contact"


async def test_a_name_nobody_has_is_answered_without_a_row(
    platform: Platform, cast: Cast
) -> None:
    """A directory miss is an answer, not an error and not the whole directory."""
    async with read_only(platform, cast.employee) as running:
        state = await running.run(
            "¿Cuál es el email de Nadie Apellido?",
            tool="get_colleague_contact",
            arguments={"name": "Nadie Apellido"},
        )

    assert state["tool_result"]["match_count"] == 0
    assert state["tool_result"]["matches"] == []
    assert state["tool_answer"]["message_key"] == "agent.tool.colleague_contact.not_found"
    assert "Ada" not in state["tool_answer"]["text"], "a miss listed somebody"


# --- 2, 4 & 8: the manager's reach --------------------------------------------


async def test_the_team_summary_reaches_the_callers_direct_reports_and_nobody_else(
    platform: Platform, cast: Cast
) -> None:
    """Checklist: 经理查本人直属下属的考勤或工时汇总, and 经理查询的范围严格限定为直属下属.

    The colleague is in the manager's own department, so the department clause would hand
    them over; the reporting relationship refuses to. They have their own punches in the
    same period, and the summary states the report's minutes and neither the colleague's
    nor their id.
    """
    today = madrid_today(datetime.now(UTC))
    window = period(today)
    await punch(platform, cast.employee, today - timedelta(days=1), hours=8)
    await punch(platform, cast.colleague, today - timedelta(days=1), hours=6)

    async with read_only(platform, cast.manager) as running:
        state = await running.run(
            "¿Cuántas horas ha fichado mi equipo?",
            tool="get_team_attendance_summary",
            arguments=window,
        )

    result = state["tool_result"]
    assert [person["employee_id"] for person in result["reports"]] == [
        cast.employee.employee_id
    ]
    assert result["worked_minutes"] == 480
    assert result["reports"][0]["worked_days"] == 1
    assert result["people"] == 1

    serialised = json.dumps(result, ensure_ascii=False)
    assert cast.colleague.employee_id not in serialised
    assert "480" in state["tool_answer"]["text"]
    assert "360" not in state["tool_answer"]["text"], "a non-report's minutes are in the answer"


async def test_an_ordinary_employee_is_refused_the_team_tool(
    platform: Platform, cast: Cast
) -> None:
    """Checklist: 普通员工调用经理工具被拒.

    Refused by the *kernel's role check*, not by this tool: the employee is not a manager,
    so `attendance.read_report` is refused before any query runs. Asserted directly on the
    kernel as well as through the graph, and asserted with the tool named explicitly so
    the refusal cannot be an artefact of the selector.
    """
    principal = await principal_of(platform, cast.employee)
    decision = can(principal, Action.ATTENDANCE_READ_REPORT)
    assert decision.allowed is False

    today = madrid_today(datetime.now(UTC))
    await punch(platform, cast.employee, today - timedelta(days=1), hours=8)

    async with read_only(platform, cast.employee) as running:
        state = await running.run(
            "¿Cuántas horas ha fichado mi equipo?",
            tool="get_team_attendance_summary",
            arguments=period(today),
        )

    assert state["tool_outcome"] == str(ToolOutcome.REFUSED)
    assert state["tool_result"] is None, "a refused tool returned values"
    assert state["tool_answer"]["message_key"] == "agent.tool.not_permitted"
    assert no_digits(state["tool_answer"]["text"])


async def test_a_stranger_is_absent_and_indistinguishable_from_an_empty_team(
    platform: Platform, cast: Cast
) -> None:
    """Checklist: 非下属返回空且不提示"存在但无权".

    Two managers, one period:

    * `manager` has a report with no punches in it, and a colleague with punches who is
      not a report;
    * `lonely_manager` — same department, managerial position — has no reports at all.

    The two answers are compared **as strings** and are equal, so nothing in the sentence
    distinguishes "there is somebody, but not yours" from "there is nothing to show". The
    colleague's id, minutes and name appear in neither the result nor the answer, and the
    answer names no permission.
    """
    today = madrid_today(datetime.now(UTC))
    window = period(today)
    # The colleague works in the manager's own department, in the same period.
    await punch(platform, cast.colleague, today - timedelta(days=1), hours=6)
    # The report exists and did not work in this window.
    await punch(platform, cast.employee, today - timedelta(days=30), hours=8)

    async with read_only(platform, cast.manager) as running:
        with_a_stranger = await running.run(
            "¿Cuántas horas ha fichado mi equipo?",
            tool="get_team_attendance_summary",
            arguments=window,
        )
    async with read_only(platform, cast.lonely_manager) as running:
        with_no_team = await running.run(
            "¿Cuántas horas ha fichado mi equipo?",
            tool="get_team_attendance_summary",
            arguments=window,
        )

    assert with_a_stranger["tool_answer"] == with_no_team["tool_answer"], (
        "an answer distinguishes 'not yours' from 'nothing to show'"
    )
    assert (
        with_a_stranger["tool_answer"]["message_key"]
        == "agent.tool.team_attendance.empty"
    )
    empty = with_no_team["tool_result"]
    assert (empty["people"], empty["punches"], empty["worked_minutes"]) == (0, 0, 0)
    assert empty["reports"] == []
    # The manager who *does* have a report still has one; what they do not have is a row
    # for the colleague, which is the whole of 「非下属返回空」.
    assert with_a_stranger["tool_result"]["people"] == 1
    assert with_a_stranger["tool_result"]["punches"] == 0

    for state in (with_a_stranger, with_no_team):
        serialised = json.dumps(state["tool_result"], ensure_ascii=False)
        assert cast.colleague.employee_id not in serialised
        assert "360" not in serialised
        assert "Ana Martín" not in serialised
        lowered = state["tool_answer"]["text"].lower()
        assert not [word for word in ORACLE_WORDS if word in lowered], state[
            "tool_answer"
        ]

    # ... and the positive control: the same manager *does* get a figure for their report,
    # so the emptiness above is the reach and not the tool being broken.
    await punch(platform, cast.employee, today - timedelta(days=1), hours=8)
    async with read_only(platform, cast.manager) as running:
        with_a_report = await running.run(
            "¿Cuántas horas ha fichado mi equipo?",
            tool="get_team_attendance_summary",
            arguments=window,
        )
    assert with_a_report["tool_result"]["worked_minutes"] == 480
    assert with_a_report["tool_answer"]["message_key"] == "agent.tool.team_attendance"
    assert cast.colleague.employee_id not in json.dumps(
        with_a_report["tool_result"], ensure_ascii=False
    )


# --- 7: a failure states no figure --------------------------------------------


async def test_a_failing_tool_states_no_figure_at_all(
    platform: Platform, cast: Cast
) -> None:
    """Checklist: 工具调用失败时…回退为"无法获取该数据"，不编造数值.

    The failure is a real one: `AttendanceService.range_view` refuses an inverted range,
    so a call with the dates the wrong way round raises inside the tool and
    `registry.invoke` states it. The answer is then asserted to contain **no digit at
    all**, in both languages — a plausible number is exactly what the ticket forbids — and
    the control below shows that a successful answer does contain its figure, so the
    assertion is not passing because answers are digit-free.
    """
    today = madrid_today(datetime.now(UTC))
    await punch(platform, cast.employee, today - timedelta(days=1), hours=8)

    async with read_only(platform, cast.employee) as running:
        failed = await running.run(
            "¿Cuántas horas he fichado?",
            tool="get_my_attendance",
            arguments={
                "from_date": (today - timedelta(days=1)).isoformat(),
                "to_date": (today - timedelta(days=2)).isoformat(),
            },
        )
        control = await running.run(
            "¿Cuántas horas he fichado?",
            tool="get_my_attendance",
            arguments=period(today),
        )

    assert failed["tool_outcome"] == str(ToolOutcome.FAILED)
    assert failed["tool_result"] is None
    assert failed["tool_answer"]["message_key"] == "agent.tool.unavailable"
    assert no_digits(failed["tool_answer"]["text"])
    for message in failed["tool_answer"].values():
        assert not any(character.isdigit() for character in message)

    assert control["tool_outcome"] == str(ToolOutcome.OK)
    assert str(control["tool_result"]["worked_minutes"]) in control["tool_answer"]["text"]


def test_the_three_constant_answers_carry_no_digits_in_either_language() -> None:
    """The same rule, asserted on the catalogue rather than on one run.

    「无法获取该数据」 is a sentence about the data being unavailable; a digit in it would be
    a figure nobody read. The check is over every locale, because a missing translation or
    a convenient default is how one would arrive.
    """
    for key in ("agent.tool.unavailable", "agent.tool.unknown", "agent.tool.not_permitted"):
        for locale, catalogue in MESSAGES.items():
            text = catalogue[key]
            assert text, (key, locale)
            assert not any(character.isdigit() for character in text), (key, locale, text)
    for key in ("agent.tool.team_attendance.empty", "agent.tool.colleague_contact.not_found"):
        for locale, catalogue in MESSAGES.items():
            assert not any(character.isdigit() for character in catalogue[key]), (key, locale)


# --- the record, and the renderer's totality ----------------------------------


async def test_the_read_only_record_names_the_tool_and_carries_no_values(
    platform: Platform, cast: Cast
) -> None:
    """`records.py`'s promise kept for the branch that now calls a tool.

    The record says *which* tool ran (§10.1's `tool_name`, the one thing a trace may say
    about a tool call) and nothing about what it was asked or returned: no argument, no
    figure, no date, and none of the answer's sentences.
    """
    today = madrid_today(datetime.now(UTC))
    window = period(today)
    await punch(platform, cast.employee, today - timedelta(days=1), hours=8)

    async with read_only(platform, cast.employee) as running:
        state = await running.run(
            "¿Cuántas horas he fichado?", tool="get_my_attendance", arguments=window
        )

    records = records_of(state)
    assert [record["node_name"] for record in records] == ["classify", "read_only_tools"]
    for record in records:
        assert set(record) == set(ALLOWED_FIELDS), record
    tool_record = records[1]
    assert tool_record["tool_name"] == "get_my_attendance"
    assert tool_record["decision"] == str(ToolOutcome.OK)
    assert tool_record["input_keys"] == ["question", "rule", "tool", "tool_arguments"]
    assert tool_record["counts"]["tools_registered"] == len(registered(ToolKind.READ_ONLY))
    assert tool_record["counts"]["result_fields"] == len(state["tool_result"])

    serialised = json.dumps(records, ensure_ascii=False)
    assert state["tool_answer"]["es"] not in serialised
    assert window["from_date"] not in serialised
    assert window["to_date"] not in serialised
    assert json.dumps(state["tool_result"], ensure_ascii=False) not in serialised


def test_every_registered_tool_has_a_renderer() -> None:
    """`render` dispatches on the tool name, so the mapping must be total.

    A registered tool with no renderer would raise a `KeyError` mid-run rather than
    answering, which is a wiring bug a test can catch before a person meets it.
    """
    assert set(rendered_tools()) == {tool.name for tool in registered()}
    for tool in registered():
        with pytest.raises(KeyError):
            # The *only* way this raises is a tool whose name is not in the mapping.
            render(ToolResult(tool=f"{tool.name}_typo", outcome=ToolOutcome.OK, data={}))


def test_the_tool_names_a_tool_can_be_reached_by_are_the_classifiers_read_only_ones() -> None:
    """The two lexical layers agree about which names exist.

    `intents.classify` decides the branch and `selection.select_tool` the tool; the names
    the graph can reach are therefore the union of what the selector produces, and every
    one of them is a registry key. Asserted from both ends, because a tool registered but
    unreachable (§6.2's `search_policy` is the deliberate exception, and it is not
    registered) and a name produced but unregistered are different bugs. The reachable set
    is compared with the *read-only* half: ticket 40's draft tools are selected by a model's
    function call (ticket 42) and not by this lexical layer, which is the seam that module
    documents.
    """
    reachable = {
        select_tool(question, today=date(2026, 9, 15)).name
        for question in (
            "¿Cuántas horas he fichado este mes?",
            "¿Cuántos días de vacaciones me quedan?",
            "¿Cuál es el estado de mi hoja de horas?",
            "¿Cuál es el email de Ana Martín?",
            "¿Cuántas horas ha fichado mi equipo este mes?",
        )
    }
    assert reachable == {tool.name for tool in registered(ToolKind.READ_ONLY)}
    with pytest.raises(UnknownTool):
        lookup("draft_reports")


def test_the_read_only_branch_is_reachable_for_the_tickets_own_example() -> None:
    """The ticket's three examples, end to end through the two lexical layers.

    「我这个月工时多少」「我还有几天年假」「昨天我几点下的班」 — a classifier that routed them
    to 制度问答, or a selector that matched no tool, would leave the tools unreachable
    however well they read.
    """
    for question, tool in (
        ("我这个月的工时是多少", "get_my_attendance"),
        ("我还有几天年假", "get_my_leave_balance"),
        ("昨天我几点下的班", "get_my_attendance"),
    ):
        from app.ai.agents.intents import classify

        assert classify(question).intent is Intent.READ_ONLY_QUERY, question
        call = select_tool(question, today=date(2026, 9, 15))
        assert call is not None and call.name == tool, (question, call)


async def test_invoke_turns_a_raise_into_a_stated_failure(platform: Platform, cast: Cast) -> None:
    """`registry.invoke` is the layer that makes a failure an outcome.

    Called directly, with the caller's real principal and a real session, so the
    conversion is asserted without a graph run — and with the tool's own `run` used, not a
    double: the range is genuinely inverted.
    """
    session = platform.factory()
    try:
        context = ToolContext(
            principal=await principal_of(platform, cast.employee),
            session=session,
            today=date(2026, 9, 15),
        )
        result = await invoke(
            ToolCall(
                name="get_my_attendance",
                arguments={"from_date": "2026-09-02", "to_date": "2026-09-01"},
            ),
            context,
        )
        assert result.outcome is ToolOutcome.FAILED
        assert result.error_type == "DomainError"
        assert result.data == {}
        assert no_digits(render(result).text)
    finally:
        await session.close()


async def test_the_tool_result_survives_the_postgres_checkpoint(
    platform: Platform, cast: Cast
) -> None:
    """The values the answer states are checkpointed, and read back by another process.

    The read-only branch is terminal, so nothing resumes it today — ticket 41 is where a
    request reads a thread's stored turn. What this pins is the property that makes that
    possible: `tool_result` and `tool_answer` are JSON-native, the real
    `langgraph-checkpoint-postgres` saver writes them, and a **different** graph object on
    a **different** connection reads the same values back. A value that was not
    serialisable, or that lived only in the node's locals, would be missing or changed
    here.
    """
    today = madrid_today(datetime.now(UTC))
    await punch(platform, cast.employee, today - timedelta(days=1), hours=8)
    principal = await principal_of(platform, cast.employee)
    thread = uuid4().hex

    async with pipeline(platform) as answers:
        session = platform.factory()
        context = AgentContext(
            principal=principal, answers=answers.service, session=session, today=today
        )
        try:
            async with open_checkpointer(get_settings(), test=True) as saver:
                first = build_graph(checkpointer=saver)
                written = await first.ainvoke(
                    {"question": "¿Cuántas horas he fichado?"},
                    thread_config(thread),
                    context=context,
                )
            async with open_checkpointer(get_settings(), test=True) as saver:
                second = build_graph(checkpointer=saver)
                snapshot = await second.aget_state(thread_config(thread))
        finally:
            await session.close()

    assert written["tool_result"]["worked_minutes"] == FULL_DAY
    assert snapshot.values["tool_result"] == written["tool_result"]
    assert snapshot.values["tool_answer"] == written["tool_answer"]
    assert snapshot.values["tool"] == "get_my_attendance"
    assert snapshot.values["tool_outcome"] == str(ToolOutcome.OK)
    assert snapshot.next == (), "the read-only branch is terminal"


# --- helpers ------------------------------------------------------------------


def no_digits(text: str) -> bool:
    """Whether a sentence states no figure at all. See the failure test."""
    return not any(character.isdigit() for character in text)


def _principal(roles: frozenset[str]):  # noqa: ANN202 - a Principal
    """A hand-built principal, for the two pure assertions about the catalogue.

    Hand-built on purpose here and nowhere else: the tests that read data resolve the real
    snapshot, and this one only asks the kernel what a role list admits.
    """
    from app.domain.access.principal import Principal

    return Principal(
        user_id=uuid4(),
        employee_id=uuid4(),
        username="someone",
        roles=roles,
        reports_employee_ids=frozenset(),
    )


def with_employee(roles: set[str]) -> Any:
    """A principal holding `roles` — and `employee`, which every account holds (ticket 08)."""
    return _principal(frozenset(roles) | {"employee"})


__all__ = ["Cast", "Running", "read_only"]
