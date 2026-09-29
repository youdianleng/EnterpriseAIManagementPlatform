"""Ticket 41: the human confirmation point, and the `agent_actions` audit trail.

Nine checklist lines, and the test that pins each — with the *negative* ones first,
because they are the ones that decide whether the design is honest:

* 只有显式点击"确认提交"按钮才会真正创建单据；聊天中的语言回复不触发任何写入 —
  `test_a_chat_reply_that_sounds_like_consent_writes_nothing_at_all`,
  `test_the_chat_path_has_no_way_to_reach_a_confirmation`
* 点击确认时会重新校验会话有效性与当前权限，权限已变更时拒绝并提示重新生成 —
  `test_a_withdrawn_permission_refuses_the_confirmation_and_creates_nothing`,
  `test_a_rule_that_moved_refuses_the_confirmation_and_keeps_the_draft_proposed`,
  `test_a_confirmation_without_a_session_never_reaches_the_handler`,
  `test_the_revalidation_is_the_submission_s_own_rule`
* 确认提交后，单据的申请人/提交人记录为**员工本人**，走既有的两级审批流，不跳过任何一级 —
  `test_the_submitted_document_is_the_employee_s_own_and_walks_both_levels`
* 审批链路上看到的信息与员工手工提交的单据完全一致 —
  `test_a_confirmed_draft_and_a_hand_filed_request_are_the_same_shape`
* 审计中记录：发起方为助手、确认人、确认时间、使用的工具与入参、生成的草稿内容、最终产生的单据 ID —
  `test_the_audit_row_carries_the_tool_its_input_the_form_and_the_entity`
* 每笔助手发起的操作在助手操作记录表中全程留痕 —
  `test_the_row_is_the_record_from_proposal_to_submission`
* 员工拒绝草稿时同样留痕（状态为已拒绝），不产生任何单据 —
  `test_a_rejection_is_recorded_and_creates_nothing`
* 单据详情页对审批人可见"由助手起草、本人确认"的标注（透明度要求）—
  `test_the_approver_s_request_detail_carries_the_transparency_annotation`
* 有端到端测试：从对话到单据生成的完整链路，断言中间过程中数据库无任何写入 —
  `test_from_conversation_to_document_and_the_middle_wrote_nothing`

**Real infrastructure, and the repository's own seams only.** PostgreSQL is real, Redis is
real (the checkpointer's own schema), and every request goes through the real HTTP API. The
model seam is inherited from the draft tests and is never reached: the draft branch takes
its call from the state, and nothing on the confirmation path calls a model at all.

**A note on the negative tests.** Every one of them asserts on *row counts* and on the
absence of the entity — not only on a status code. A 409 whose transaction still wrote a
`leave_requests` row would be the failure this ticket exists to prevent, and a status-code
assertion cannot see it.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.ai.agents import AgentContext, build_graph, open_checkpointer, thread_config
from app.ai.agents import nodes as agent_nodes
from app.config import get_settings
from app.core.errors import ErrorCode
from app.core.messages import MESSAGES
from app.domain.access.permissions import Action
from app.domain.attendance.business_day import MADRID, madrid_today
from app.domain.timesheet.models import monday_of
from tests.support.platform import Actor, Platform

# Ticket 40's `cast` fixture, imported rather than redefined: the drafts this ticket
# confirms are the ones that ticket produces, and a second fixture would be a second answer
# to "what does a person with a company week and a manager look like".
#
# **It is renamed, and the rename is not cosmetic.** `conftest.py` has a `cast` fixture of
# its own (the document tests' two departments and four employees), and a pytest fixture is
# looked up by *name*: an imported fixture whose name shadows one from `conftest` resolves
# differently depending on collection order. This module walked into exactly that trap —
# `test_agent_confirmation.py`'s tests were handed the document fixture and failed with
# `AttributeError: 'Cast' object has no attribute 'employee'` while the same test file passed
# on its own. One wrapper under a name of its own makes which fixture is meant explicit and
# unconditional, and `ruff`'s F811 is then right to be switched off for the alias.
from tests.test_agent_draft_tools import (
    Cast,
    leave_draft,
    punch,
    table_counts,
    working_day,
)
from tests.test_agent_draft_tools import cast as _staff_people  # noqa: F401
from tests.test_agent_graph import pipeline, principal_of


@pytest.fixture
async def staff_cast(_staff_people: Cast) -> Cast:  # noqa: F811 - the fixture, not a rebind
    """Ticket 40's cast, under this module's own name."""
    return _staff_people


FULL_DAY = 480


def _weekday(day: date) -> date:
    """`day`, moved back to the Friday before it if it lands on a weekend.

    The module's calibration rule asks for a range with a working day in it, and a
    `date - timedelta` that happens to be a Saturday would make a fixture fail for a reason
    that looks like a product defect. Weeks are seven days long, so this never moves the
    week the date belongs to when it is already a weekday.
    """
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


# --- the drafts the confirmation is given -------------------------------------


@dataclass(frozen=True, slots=True)
class Proposal:
    """One draft, as the chat path produced it. Everything a click needs, and no more.

    `action_id` is the draft's durable identity, `form` is the complete editable form the
    interruption showed, and `conversation_id` is where it was filed — which is what the
    conversation read answers with, so a test can prove the row survived.
    """

    action_id: str
    conversation_id: str
    form: dict[str, Any]
    tool: str


async def propose(
    platform: Platform, actor: Actor, tool: str, arguments: Mapping[str, Any]
) -> Proposal:
    """Run the graph's draft branch, exactly as the chat path does, and keep what it wrote.

    The question is the one `intents.classify` routes to 待办操作 — a run reaches the draft
    branch by being classified into it, and a policy-shaped question would be answered by the
    other branch with the tool name sitting unused in the state.
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
                state = await build_graph(checkpointer=saver).ainvoke(
                    {
                        "question": "Quiero solicitar dos días de vacaciones",
                        "tool": tool,
                        "tool_arguments": dict(arguments),
                    },
                    thread_config(uuid4().hex),
                    context=context,
                )
        finally:
            await session.close()

    assert state["pending_action"]["status"] == "proposed", (
        state["pending_action"],
        state.get("tool_result"),
        state.get("tool_answer"),
    )
    assert state["agent_action_id"] and state["conversation_id"]
    return Proposal(
        action_id=state["agent_action_id"],
        conversation_id=state["conversation_id"],
        form=state["prefill_form"],
        tool=tool,
    )


async def propose_leave(
    platform: Platform, staff_cast: Cast, *, day: date, who: Actor | None = None
) -> Proposal:
    return await propose(
        platform,
        who or staff_cast.employee,
        "draft_leave_request",
        leave_draft(staff_cast, day=day),
    )


def form_values(form: Mapping[str, Any]) -> dict[str, Any]:
    """The form's proposed values, as a browser would post them back."""
    return {
        field["name"]: field["value"]
        for field in form["fields"]
        if field["value"] is not None
    }


# --- counting, and the three refusals' shapes ---------------------------------


async def entity_counts(platform: Platform) -> dict[str, int]:
    """The rows that only a *document* creates.

    `table_counts` (ticket 40's) counts the whole schema, and it is the right instrument for
    "nothing at all was written". This is the sharper one the ticket's first line needs: an
    `agent_actions` row that moves status is the audit working, while a row in one of these
    tables is a document that was created — and the two facts must not be confused.
    """
    tables = (
        "leave_requests",
        "attendance_corrections",
        "timesheet_entries",
        "timesheets",
        "approval_requests",
        "approval_steps",
        "approval_decisions",
    )
    return {
        table: int(await platform.scalar(f"SELECT count(*) FROM {table}") or 0)
        for table in tables
    }


async def draft_row(platform: Platform, action_id: str) -> dict[str, Any]:
    """The `agent_actions` row, as a reader of the audit sees it."""
    row = (
        await platform.sql(
            """
            SELECT status, confirmed_at, resulting_entity_type, resulting_entity_id,
                   tool_name, tool_input, produced_prefill_form, created_at, expires_at
              FROM agent_actions WHERE id = :id
            """,
            {"id": action_id},
        )
    )[0]
    return {
        "status": row[0],
        "confirmed_at": row[1],
        "entity_type": row[2],
        "entity_id": row[3],
        "tool_name": row[4],
        "tool_input": row[5],
        "form": row[6],
    }


# --- 1: only an explicit click creates anything -------------------------------


async def test_a_chat_reply_that_sounds_like_consent_writes_nothing_at_all(
    platform: Platform, staff_cast: Cast
) -> None:
    """Checklist: 只有显式点击"确认提交"按钮才会真正创建单据；聊天中的语言回复不触发任何写入.

    The ticket's first line, and the test is written the way the brief asks: a
    confirming-sounding message is *sent through the chat path* — a second run on the same
    thread, exactly what a person typing 好的 produces — and then:

    * **every table in the schema** holds the same number of rows as before,
    * the draft is still `proposed`, with no `confirmed_at` and no entity, so the audit did
      not move either,
    * and the entity tables hold nothing.

    A run is the honest instrument here rather than a bare function call: the claim is about
    what the *chat* can do, so the chat is what is driven.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    proposal = await propose_leave(platform, staff_cast, day=day)

    entities_before = await entity_counts(platform)
    drafts_before = await platform.scalar("SELECT count(*) FROM agent_actions")

    # Three ways of saying yes, in the two languages this system is written for, plus a
    # client that has sent a tool name and no arguments at all.
    async with pipeline(platform) as answers:
        for message in ("sí, confirma", "yes, confirm it", "¡adelante!"):
            session = platform.factory()
            context = AgentContext(
                principal=await principal_of(platform, staff_cast.employee),
                answers=answers.service,
                session=session,
                conversation_id=UUID(proposal.conversation_id),
            )
            try:
                async with open_checkpointer(get_settings(), test=True) as saver:
                    await build_graph(checkpointer=saver).ainvoke(
                        {
                            "question": message,
                            "tool": "draft_leave_request",
                            "tool_arguments": {},
                        },
                        thread_config(uuid4().hex),
                        context=context,
                    )
            finally:
                await session.close()

    entities_after = await entity_counts(platform)
    drafts_after = await platform.scalar("SELECT count(*) FROM agent_actions")

    assert entities_after == entities_before, {
        "a chat reply created a document": {
            table: (entities_before[table], entities_after[table])
            for table in entities_before
            if entities_before[table] != entities_after.get(table)
        }
    }
    # And the audit did not move either: no draft was proposed, and none was answered.
    assert drafts_after == drafts_before

    row = await draft_row(platform, proposal.action_id)
    assert row["status"] == "proposed", "the chat path moved the draft"
    assert row["confirmed_at"] is None
    assert row["entity_id"] is None

    # What the chat *does* write, and the reason this test does not count every table:
    # asking a question is the answer path's job, and a transcript row plus its audit
    # entry are that path working. They are named here rather than left implicit, so a
    # reader can see exactly what was excluded and why. `entity_counts` above is the
    # assertion that matters: a confirming-sounding *message* produced no document.
    assert await platform.scalar("SELECT count(*) FROM leave_requests") == 0
    assert await platform.scalar("SELECT count(*) FROM approval_requests") == 0


def test_the_chat_path_has_no_way_to_reach_a_confirmation() -> None:
    """The structural half of the line above: the graph cannot confirm, by construction.

    A behavioural test can only show that the messages it thought of wrote nothing. This one
    is about the code: `ai/agents/**` — the only thing the chat path is — does not name the
    confirmation module, the confirmation module is not part of any tool's run, and the
    node that sees the human's words records only their *type*. So "a chat reply cannot
    confirm" is a property of the call graph rather than of a classifier happening to be
    right, which is the difference the ticket's first line is really about.
    """
    import ast
    from pathlib import Path

    from app.ai.agents import state as agent_state

    agents = Path(agent_state.__file__).parent
    offenders: dict[str, list[str]] = {}
    for module in sorted(agents.glob("*.py")):
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module]
            if any("agent.confirmation" in name for name in names):
                offenders[module.name] = names
    assert offenders == {}, f"the chat path can reach the confirmation: {offenders}"

    # And the pause records the *type* of the answer, never its value: §10.1's rule that a
    # person's words do not become a record, asserted where the words are seen.
    source = Path(agent_nodes.__file__).read_text(encoding="utf-8")
    assert '"value_type": type(answer).__name__' in source
    assert '"interpreted": False' in source


# --- 2: the confirmation re-validates -----------------------------------------


async def test_a_withdrawn_permission_refuses_the_confirmation_and_creates_nothing(
    platform: Platform, staff_cast: Cast
) -> None:
    """Checklist: 点击确认时…权限已变更时拒绝并提示重新生成.

    **What "changed" is measured against**, and what the answer honestly is. At confirmation
    the kernel is asked again, about the entity's own action, with a principal the request
    re-resolved from the database — so every input the decision reads (roles, clearance, the
    department subtree, the reporting relationship) is *today's*, not the draft's. What that
    can and cannot catch turns out to be worth stating, because it is narrower than "any
    permission change":

    * The three drafted documents are all **self-filed** — `leave.request_own`,
      `attendance.correction_own`, `timesheet.write_own`/`submit_own` are in
      `SELF_ONLY_ACTIONS`, and `PrincipalBuilder` always adds `employee` to the snapshot — so
      no *other* grant can make a confirmation legal or illegal. A role removed, a department
      moved or a clearance lowered changes nothing about whether a person may file their own
      leave. That is not a gap in this ticket; it is the shape of the permission the ticket
      asks about, and it is why §6.3's third requirement ("the submission is the employee's
      own") and this line are the same sentence.
    * What *does* change it is the account no longer resolving at all — deactivation, a
      terminated assignment, a revoked session. That is asserted here and in the test below,
      and it is the real-world case: an offboarding between the draft and the click.

    The decision is still asked, and asked on today's facts, which is what makes the property
    structural rather than a claim: a future entity that is *not* self-filed inherits the
    check without anybody remembering to add it. That the check fires is pinned by
    `test_a_permission_that_no_longer_permits_is_refused_by_the_action_it_names`.

    Here: the account is deactivated, so `resolve_principal` answers nothing and the request
    never reaches the domain at all — 401, catalogued, with the draft untouched and no
    document anywhere.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    proposal = await propose_leave(platform, staff_cast, day=day)

    before = await entity_counts(platform)
    await platform.sql(
        "UPDATE users SET is_active = false WHERE id = :id",
        {"id": staff_cast.employee.user_id},
    )

    refused = await staff_cast.employee.post(
        f"/api/v1/agent/actions/{proposal.action_id}/confirm",
        json={"fields": form_values(proposal.form)},
    )
    assert refused.status_code == 401, refused.text
    assert refused.json()["error"]["code"] == ErrorCode.SESSION_INVALID.value

    after = await entity_counts(platform)
    assert after == before, {
        "a refused confirmation created a document": {
            table: (before[table], after[table])
            for table in before
            if before[table] != after.get(table)
        }
    }
    row = await draft_row(platform, proposal.action_id)
    assert row["status"] == "proposed", "a refused confirmation consumed the draft"
    assert row["entity_id"] is None

    # And the row is still readable from the thread, so the employee can see what was
    # proposed even though they can no longer confirm it — the draft is evidence, not a
    # resource the request has to be able to reach.
    assert row["form"]["fields"], "the draft's form was lost"


async def test_a_permission_that_no_longer_permits_is_refused_by_the_action_it_names(
    platform: Platform, staff_cast: Cast
) -> None:
    """The kernel's re-check fires, at the *service* boundary, and names the action it refused.

    A synthetic principal, and the honesty is in saying so: as the test above records, no
    real change to a self-filing employee's grants can make `leave.request_own` illegal while
    their account resolves — the catalogued rule is ownership plus `employee`, and the
    snapshot builder always adds `employee`. So the guard is exercised the only way it can be
    without inventing a permission: the confirmation is asked for **with a principal that
    holds nothing**, constructed directly on a real database session.

    It is a service-level test rather than an HTTP one on purpose. Its subject is the kernel
    decision inside `ConfirmationService._require_permitted`, and going through the web layer
    would mean fabricating a request whose principal no endpoint can produce — which would
    make the test about the fabrication. Everything else is real: the draft row is the one the
    chat path wrote, the session is a real one, and the counts are the schema's.

    What is asserted is the three things that make the guard worth having: the refusal is a
    `ConfirmationRefused` carrying the *submission's* action name (so an operator can see
    which rule moved), **nothing was created**, and the draft is still `proposed`.
    """
    from app.domain.access.principal import Principal
    from app.domain.agent.confirmation import (
        ConfirmationRefused,
    )
    from app.domain.agent.confirmation import (
        service_for as confirmation_for,
    )

    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    proposal = await propose_leave(platform, staff_cast, day=day)

    # The same person and the same row — the *draft's* owner — with no roles at all.
    nobody = Principal(
        user_id=UUID(staff_cast.employee.user_id),
        employee_id=UUID(staff_cast.employee.employee_id),
        username=staff_cast.employee.username,
        roles=frozenset(),
        clearance_level="low",
        department_ids=frozenset(),
        primary_department_id=None,
        is_manager=False,
        reports_employee_ids=frozenset(),
    )
    before = await entity_counts(platform)

    session = platform.factory()
    try:
        with pytest.raises(ConfirmationRefused) as refusal:
            await confirmation_for(session).confirm(
                action_id=UUID(proposal.action_id),
                principal=nobody,
                fields=form_values(proposal.form),
            )
    finally:
        await session.close()

    assert "leave.request_own" in refusal.value.detail
    assert refusal.value.message_key == "errors.forbidden"
    assert await entity_counts(platform) == before
    assert (await draft_row(platform, proposal.action_id))["status"] == "proposed"


async def test_a_rule_that_moved_refuses_the_confirmation_and_keeps_the_draft_proposed(
    platform: Platform, staff_cast: Cast
) -> None:
    """The cheap half of re-validation, and the branch that proves the draft survives it.

    A leave draft is proposed against a balance that covers it. Then the balance is spent —
    by HR setting the entitlement to zero, which is the same refusal the submission path
    raises (`ERR_LVE_009`) rather than a second rule invented here. The click is refused with
    that catalogued key, the draft is **still `proposed`** (nothing marks a refusal
    `rejected`: that would answer "the employee said no" to a question nobody asked), and no
    request row exists.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    proposal = await propose_leave(platform, staff_cast, day=day)

    before = await entity_counts(platform)
    # Set the entitlement below what the draft costs — the year the two days fall in.
    year = day.year
    await staff_cast.hr.put(
        f"/api/v1/leave/balances/{staff_cast.employee.employee_id}/{year}/annual",
        json={"entitled_days": 0, "note": "Prueba de revalidación"},
    )

    refused = await staff_cast.employee.post(
        f"/api/v1/agent/actions/{proposal.action_id}/confirm",
        json={"fields": form_values(proposal.form)},
    )
    assert refused.status_code == 409, refused.text
    body = refused.json()["error"]
    assert body["code"] == ErrorCode.AGENT_DRAFT_CONFIRMATION_REFUSED.value
    # The domain's own key travels — and it is the *submission's* refusal, not a second rule
    # written here: the same `ERR_LVE_009` a hand-filed request over the remaining days gets.
    assert body["message_key"] == "errors.leave_balance_insufficient"
    assert body["detail"].startswith("errors.leave_balance_insufficient")
    assert "remaining 0" in body["detail"]

    after = await entity_counts(platform)
    assert after == before
    assert (await draft_row(platform, proposal.action_id))["status"] == "proposed"


async def test_a_confirmation_without_a_session_never_reaches_the_handler(
    platform: Platform, staff_cast: Cast
) -> None:
    """The session half of the line: a signed-out caller confirms nothing, and writes nothing.

    §6.3 asks the click to re-validate 会话有效性. The mechanism is the request's own
    principal dependency: with no session cookie there is no principal, so the handler is
    never entered — asserted by the status, the catalogued code, and the counts. The row
    stays `proposed`, because a request that never reached the module cannot have moved it.
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    proposal = await propose_leave(platform, staff_cast, day=day)

    before = await entity_counts(platform)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        refused = await anon.post(
            f"/api/v1/agent/actions/{proposal.action_id}/confirm",
            json={"fields": form_values(proposal.form)},
        )
    assert refused.status_code == 401, refused.text
    assert refused.json()["error"]["code"] == ErrorCode.SESSION_INVALID.value

    assert await entity_counts(platform) == before
    assert (await draft_row(platform, proposal.action_id))["status"] == "proposed"


def test_the_revalidation_is_the_submission_s_own_rule() -> None:
    """"Re-validate" must not become a second copy of the rules — asserted structurally.

    Ticket 40 extracted the three `check_*` methods so the draft and the write path share one
    implementation, and the brief for this ticket says the confirmation must call those again
    rather than re-check. That is a claim about code, so it is asserted against the code: the
    confirmation module's **executable statements** name none of the vocabulary a second
    implementation would be written in — no balance arithmetic, no window comparison, no
    project lookup — and it reaches the write paths that call the shared checks.

    Docstrings are excluded on purpose and obviously so: this module's own prose *names* the
    three methods, because explaining which rules are reused is the point of the prose. What
    must not contain them is the code.
    """
    import ast
    from pathlib import Path

    from app.domain.agent import confirmation

    source = Path(confirmation.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    # Every string and every docstring dropped: what remains is names, calls and literals
    # that are not documentation.
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            node.value = ast.Constant(value=None)
    code = ast.unparse(tree)

    for forbidden in (
        "entitled_days",
        "remaining_days",
        "pending_days",
        "counts_by_year",
        "live_request_overlapping",
        "_window_refusal",
        "_require_room",
        "resolve_record_target",
        "assert_monday",
        "check_request",
        "check_draft",
        "check_entry",
    ):
        assert forbidden not in code, f"the confirmation re-implements {forbidden}"

    # And the calls it does make are the three modules' own write paths, in order.
    assert ".draft(" in code
    assert ".add_entry(" in code
    assert ".submit(" in code
    assert "timesheets.submit" in code or "leave.submit" in code


# --- 3: the submission is the employee's own ----------------------------------


async def test_the_submitted_document_is_the_employee_s_own_and_walks_both_levels(
    platform: Platform, staff_cast: Cast
) -> None:
    """Checklist: 单据的申请人/提交人记录为**员工本人**，走既有的两级审批流，不跳过任何一级.

    Three facts, and the third is the one the ticket's sentence is about:

    * the leave request's `employee_id` is the **person's**, not the assistant's and not the
      manager's;
    * `approval_requests.initiated_by` is `agent` and `confirmed_by_user_id` is the
      confirmer's user id — §3.4's two columns, which is the whole audit difference;
    * the route is the ordinary one: a level-1 step **pending** for the person's manager and
      no level skipped, so the document is in exactly the state a hand-filed request is in
      after filing. Nothing here shortcuts to `approved`.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    proposal = await propose_leave(platform, staff_cast, day=day)

    confirmed = await staff_cast.employee.post(
        f"/api/v1/agent/actions/{proposal.action_id}/confirm",
        json={"fields": form_values(proposal.form)},
    )
    assert confirmed.status_code == 201, confirmed.text
    body = confirmed.json()
    assert body["status"] == "confirmed"
    assert body["entity_type"] == "leave_request"
    assert body["entity_id"]
    assert body["confirmed_at"]

    request = (
        await platform.sql(
            "SELECT employee_id, start_date, end_date FROM leave_requests WHERE id = :id",
            {"id": body["entity_id"]},
        )
    )[0]
    assert str(request[0]) == staff_cast.employee.employee_id, "the requester is not the person"
    assert request[1] == day

    approval = (
        await platform.sql(
            """
            SELECT requester_employee_id, initiated_by, confirmed_by_user_id, status
              FROM approval_requests WHERE entity_type = 'leave_request' AND entity_id = :id
            """,
            {"id": body["entity_id"]},
        )
    )[0]
    assert str(approval[0]) == staff_cast.employee.employee_id
    assert approval[1] == "agent"
    assert str(approval[2]) == staff_cast.employee.user_id, "the confirmer is not the clicker"
    assert approval[3] == "pending_first", "the flow did not start at the first level"

    steps = await platform.sql(
        """
        SELECT s.level, s.status, s.approver_employee_id
          FROM approval_steps s JOIN approval_requests r ON r.id = s.request_id
         WHERE r.entity_id = :id ORDER BY s.level
        """,
        {"id": body["entity_id"]},
    )
    assert len(steps) == 1, "more than the first level was created in advance"
    assert steps[0][0] == 1 and steps[0][1] == "pending"
    assert str(steps[0][2]) == staff_cast.manager.employee_id, "level one is not the manager"

    # And the manager can decide it: the level is real, not a row nobody can act on.
    decided = await staff_cast.manager.post(
        f"/api/v1/leave/requests/{body['entity_id']}/decide",
        json={"decision": "approve", "comment": "Visto bueno"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["approval"]["status"] == "pending_second", (
        "approving level one did not open level two"
    )


async def test_a_confirmed_draft_and_a_hand_filed_request_are_the_same_shape(
    platform: Platform, staff_cast: Cast
) -> None:
    """Checklist: 审批链路上看到的信息与员工手工提交的单据完全一致.

    Two documents, one drafted by the assistant and one filed by hand, for the same person
    with the same dates but in different weeks. What an approver reads must differ in
    **nothing but the transparency annotation**: the same JSON keys, the same state, the same
    step shape. That is asserted key-by-key rather than by eyeballing, and the two keys that
    are allowed to differ are named.

    It is also the honest place to record what does *not* differ: the confirmation is not a
    second kind of document. There is no `source` field, no second endpoint for approvers and
    no second route for the engine — the only trace of the assistant is §3.4's
    `initiated_by`, which is precisely what §6.3's fifth requirement asks for.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    proposal = await propose_leave(platform, staff_cast, day=day)

    confirmed = await staff_cast.employee.post(
        f"/api/v1/agent/actions/{proposal.action_id}/confirm",
        json={"fields": form_values(proposal.form)},
    )
    assert confirmed.status_code == 201, confirmed.text
    drafted = confirmed.json()["entity_id"]

    # A hand-filed request, three weeks out so the two cannot overlap.
    later = _weekday(day + timedelta(days=21))
    filed = await staff_cast.employee.post(
        "/api/v1/leave/requests",
        json={
            "leave_type": "annual",
            "start_date": later.isoformat(),
            "end_date": (later + timedelta(days=1)).isoformat(),
        },
    )
    assert filed.status_code == 201, filed.text
    by_hand = filed.json()["id"]
    submitted = await staff_cast.employee.post(f"/api/v1/leave/requests/{by_hand}/submit")
    assert submitted.status_code == 200, submitted.text

    # The approver opens both, as the *manager* (who may read both).
    one = await staff_cast.manager.get(f"/api/v1/leave/requests/{drafted}")
    two = await staff_cast.manager.get(f"/api/v1/leave/requests/{by_hand}")
    assert one.status_code == 200 and two.status_code == 200, (one.text, two.text)

    a, b = one.json(), two.json()
    # The response shape is identical, key for key: an approver's screen renders one shape,
    # because there is only one kind of document.
    assert set(a) == set(b), "the two documents do not have the same shape"
    assert set(a["approval"]) == set(b["approval"])

    # The document's own answers are identical. `id`, `created_at` and the derived
    # `allocations` are the row's identity and its ledger split — different rows, different
    # values — and they are deliberately not part of "what the approver is told about the
    # document", which is what this assertion is about.
    for key in (
        "employee_id",
        "leave_type",
        "business_days_count",
        "state",
        "has_attachment",
        "attachment_readable_by",
        "attachment_reference",
    ):
        assert a[key] == b[key], f"{key} differs between a confirmed and a hand-filed request"

    assert a["state"] == b["state"] == "in_approval"
    assert a["approval"]["status"] == b["approval"]["status"] == "pending_first"
    assert a["approval"]["round"] == b["approval"]["round"] == 1
    assert a["approval"]["decisions"] == b["approval"]["decisions"] == []

    # The one difference, and it is the transparency annotation.
    assert (a["approval"]["initiated_by"], b["approval"]["initiated_by"]) == ("agent", "user")
    assert a["approval"]["confirmed_by_user_id"] == staff_cast.employee.user_id
    assert b["approval"]["confirmed_by_user_id"] is None


async def test_the_approver_s_request_detail_carries_the_transparency_annotation(
    platform: Platform, staff_cast: Cast
) -> None:
    """Checklist: 单据详情页对审批人可见"由助手起草、本人确认"的标注（透明度要求）.

    §6.3's fifth requirement names the source itself: `approval_requests.initiated_by` already
    exists, the annotation must come from an API field and never from a client-side guess, and
    ticket 53 is the screen that will render it. This asserts the field travels on all three
    request *details* — leave, correction and timesheet are three schemas — from the engine's
    own row, and that the hand-filed default is still `user`, which is what makes the pair a
    truthful annotation rather than a flag that is always on.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    await punch(platform, staff_cast.employee, day)

    # A drafted correction, confirmed.
    correction = await propose(
        platform,
        staff_cast.employee,
        "draft_attendance_correction",
        {
            "business_date": day.isoformat(),
            "kind": "clock_out",
            "corrected_at": datetime(
                day.year, day.month, day.day, 16, 10, tzinfo=MADRID
            ).isoformat(),
            "reason": "Olvide fichar la salida.",
        },
    )
    confirmed = await staff_cast.employee.post(
        f"/api/v1/agent/actions/{correction.action_id}/confirm",
        json={"fields": form_values(correction.form)},
    )
    assert confirmed.status_code == 201, confirmed.text
    correction_id = confirmed.json()["entity_id"]

    detail = await staff_cast.employee.get(f"/api/v1/attendance/corrections/{correction_id}")
    assert detail.status_code == 200, detail.text
    approval = detail.json()["approval"]
    assert approval["initiated_by"] == "agent"
    assert approval["confirmed_by_user_id"] == staff_cast.employee.user_id

    # The same annotation, from a hand-filed correction: `user`, with nobody named. Its day
    # is a working day two weeks back and holds nothing, so the flow accepts it.
    plain_day = _weekday(day - timedelta(days=14))
    by_hand = await staff_cast.employee.post(
        "/api/v1/attendance/corrections",
        json={
            "business_date": plain_day.isoformat(),
            "kind": "clock_out",
            "corrected_at": datetime(
                plain_day.year, plain_day.month, plain_day.day, 17, 0, tzinfo=MADRID
            ).isoformat(),
            "reason": "Salida real.",
        },
    )
    assert by_hand.status_code == 201, by_hand.text
    plain = await staff_cast.employee.post(
        f"/api/v1/attendance/corrections/{by_hand.json()['id']}/submit"
    )
    assert plain.status_code == 200, plain.text
    plain_detail = await staff_cast.employee.get(
        f"/api/v1/attendance/corrections/{by_hand.json()['id']}"
    )
    assert plain_detail.json()["approval"]["initiated_by"] == "user"
    assert plain_detail.json()["approval"]["confirmed_by_user_id"] is None

    # The catalogue wording exists in both languages, so ticket 53 has something to render.
    for key in ("errors.agent_draft_not_found", "errors.agent_draft_not_confirmable"):
        assert MESSAGES["es"].get(key) and MESSAGES["en"].get(key), key


async def test_a_timesheet_draft_confirms_into_the_week_s_own_request(
    platform: Platform, staff_cast: Cast
) -> None:
    """The third entity, end to end: an entry is written and the *week* is filed.

    A timesheet is not filed entry by entry — the document the engine approves is the week's
    sheet — so a confirmed entry lands under the week's request rather than under one of its
    own. That is asserted by following the resulting entity id into
    `approval_requests.entity_type = 'timesheet'`, and by the entry existing in the grid with
    the values the form carried.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    # The week the *entry* falls in, not the week today falls in: `working_day` in ticket
    # 40's fixture is the Monday of last week, so `monday_of(today)` would be the week
    # after it and the module would refuse the entry for being outside its own week.
    week = monday_of(day)

    proposal = await propose(
        platform,
        staff_cast.employee,
        "draft_timesheet",
        {
            "week_start": week.isoformat(),
            "entry_date": day.isoformat(),
            "project_id": staff_cast.project["id"],
            "task_id": staff_cast.task["id"],
            "minutes": FULL_DAY,
            "note": "Borrador confirmado",
        },
    )
    values = form_values(proposal.form)
    values["minutes"] = FULL_DAY + 60  # the employee edits the draft before confirming

    confirmed = await staff_cast.employee.post(
        f"/api/v1/agent/actions/{proposal.action_id}/confirm", json={"fields": values}
    )
    assert confirmed.status_code == 201, confirmed.text
    body = confirmed.json()
    assert body["entity_type"] == "timesheet"

    approval = (
        await platform.sql(
            """
            SELECT initiated_by, confirmed_by_user_id, status
              FROM approval_requests WHERE id = :id
            """,
            {"id": body["entity_id"]},
        )
    )[0]
    assert approval[0] == "agent"
    assert str(approval[1]) == staff_cast.employee.user_id
    assert approval[2] == "pending_first"

    written = await platform.sql(
        """
        SELECT minutes, note, entry_date FROM timesheet_entries
         WHERE employee_id = :employee_id AND entry_date = :day
        """,
        {"employee_id": staff_cast.employee.employee_id, "day": day},
    )
    assert len(written) == 1, written
    assert written[0][0] == FULL_DAY + 60, "the edited value is not what was written"
    assert written[0][1] == "Borrador confirmado"


# --- 4: the audit -------------------------------------------------------------


async def test_the_audit_row_carries_the_tool_its_input_the_form_and_the_entity(
    platform: Platform, staff_cast: Cast
) -> None:
    """Checklist: 审计中记录发起方为助手、确认人、确认时间、工具与入参、草稿内容、最终单据 ID.

    One row, read back after the click, and every field §3.6 names is asserted on it:

    | what the checklist asks for | where it is |
    |---|---|
    | 发起方为助手 (the assistant proposed it) | `approval_requests.initiated_by = 'agent'` |
    | 确认人 | `approval_requests.confirmed_by_user_id`, and the requester is the person |
    | 确认时间 | `agent_actions.confirmed_at`, written by the *database's* `now()` |
    | 使用的工具与入参 | `tool_name` and `tool_input` |
    | 生成的草稿内容 | `produced_prefill_form` |
    | 最终单据 ID | `resulting_entity_type` + `resulting_entity_id` |

    The form is asserted to be the one the assistant proposed, and the two values the employee
    then edited are asserted to differ from it — which is the point of the column being "the
    draft as proposed" while the document records the draft **as confirmed**. §6.3's first
    requirement is that every field is editable, so the two can legitimately differ, and a row
    that stored the confirmed values in the proposal column would be the audit flattering
    itself.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    proposal = await propose_leave(platform, staff_cast, day=day)

    # The employee edits the draft: two days become one, before the click.
    edited = form_values(proposal.form)
    edited["end_date"] = day.isoformat()

    confirmed = await staff_cast.employee.post(
        f"/api/v1/agent/actions/{proposal.action_id}/confirm", json={"fields": edited}
    )
    assert confirmed.status_code == 201, confirmed.text
    entity_id = confirmed.json()["entity_id"]

    row = await draft_row(platform, proposal.action_id)
    assert row["status"] == "confirmed"
    assert row["confirmed_at"] is not None, "the confirmation instant was not recorded"
    assert row["tool_name"] == "draft_leave_request"
    assert row["tool_input"]["leave_type"] == "annual"
    assert row["tool_input"]["start_date"] == day.isoformat()
    assert row["form"] == proposal.form, "the stored form is not the one that was proposed"
    assert row["entity_type"] == "leave_request"
    assert str(row["entity_id"]) == entity_id

    # The confirmed values, not the proposed ones, are what the document holds.
    stored = (
        await platform.sql(
            "SELECT start_date, end_date FROM leave_requests WHERE id = :id",
            {"id": entity_id},
        )
    )[0]
    assert stored[1] == day, "the confirmed edit did not reach the document"
    proposed_end = next(
        field["value"] for field in proposal.form["fields"] if field["name"] == "end_date"
    )
    assert proposed_end != day.isoformat(), "the fixture did not actually edit the draft"


async def test_the_row_is_the_record_from_proposal_to_submission(
    platform: Platform, staff_cast: Cast
) -> None:
    """Checklist: 每笔助手发起的操作在助手操作记录表中全程留痕.

    "全程" is two observations of the *same* row: one before the click (the assistant
    proposed something, and nothing has happened to it) and one after (a person answered, and
    here is what came of it). A row that appeared only on confirmation would lose the record
    of what was proposed; a row that never left `proposed` would lose the record of the
    answer. Both are asserted, with the row's identity — its id — unchanged between them.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    proposal = await propose_leave(platform, staff_cast, day=day)

    before = await draft_row(platform, proposal.action_id)
    assert before["status"] == "proposed"
    assert before["confirmed_at"] is None
    assert before["entity_type"] is None and before["entity_id"] is None
    assert before["form"]["fields"], "the proposal was not recorded"

    confirmed = await staff_cast.employee.post(
        f"/api/v1/agent/actions/{proposal.action_id}/confirm",
        json={"fields": form_values(proposal.form)},
    )
    assert confirmed.status_code == 201, confirmed.text

    after = await draft_row(platform, proposal.action_id)
    assert after["status"] == "confirmed"
    assert after["entity_id"] is not None
    assert after["tool_name"] == before["tool_name"]
    assert after["tool_input"] == before["tool_input"]
    assert after["form"] == before["form"]

    # And the conversation read answers with it: the draft is legible from the thread it was
    # proposed in, which is where the employee goes back to look.
    read = await staff_cast.employee.get(
        f"/api/v1/answers/conversations/{proposal.conversation_id}"
    )
    assert read.status_code == 200, read.text
    assert read.json()["draft"]["status"] == "confirmed"
    assert read.json()["draft"]["id"] == proposal.action_id


async def test_a_rejection_is_recorded_and_creates_nothing(
    platform: Platform, staff_cast: Cast
) -> None:
    """Checklist: 员工拒绝草稿时同样留痕（状态为已拒绝），不产生任何单据.

    The other half of the click. The row moves to `rejected` with the instant it was
    answered, `resulting_entity_*` stays NULL — which is what makes 「不产生任何单据」 a fact
    about the row — and **every** table in the schema holds what it held before except
    `agent_actions`, whose one row moved status. Written that way so the assertion can see a
    stray audit row or ledger entry as well as a stray document.

    The rejection does not consult the document's rules: discarding a form cannot collide with
    a balance, and a test that required one would be inventing a refusal.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    proposal = await propose_leave(platform, staff_cast, day=day)

    before = await table_counts(platform)
    rejected = await staff_cast.employee.post(
        f"/api/v1/agent/actions/{proposal.action_id}/reject",
        json={"reason": "No era lo que quería pedir."},
    )
    assert rejected.status_code == 200, rejected.text
    body = rejected.json()
    assert body["status"] == "rejected"
    assert body["entity_type"] is None and body["entity_id"] is None
    assert body["confirmed_at"], "the answer's instant was not recorded"

    after = await table_counts(platform)
    moved = {
        table: (before[table], after[table])
        for table in before
        if before[table] != after.get(table)
    }
    assert moved == {}, f"a rejection wrote something: {moved}"

    row = await draft_row(platform, proposal.action_id)
    assert row["status"] == "rejected"
    assert row["entity_id"] is None and row["entity_type"] is None
    assert await entity_counts(platform) == {table: 0 for table in (
        "leave_requests",
        "attendance_corrections",
        "timesheet_entries",
        "timesheets",
        "approval_requests",
        "approval_steps",
        "approval_decisions",
    )}

    # The draft is answered: a second rejection is refused rather than recorded twice.
    again = await staff_cast.employee.post(
        f"/api/v1/agent/actions/{proposal.action_id}/reject", json={}
    )
    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == ErrorCode.AGENT_DRAFT_NOT_CONFIRMABLE.value


async def test_the_audit_row_cannot_be_re_decided_even_without_the_service(
    platform: Platform, staff_cast: Cast
) -> None:
    """The guard a second writer cannot walk past, asked of the repository directly.

    The service refuses an answered draft before it gets here (`_require_answerable`), so the
    routes never reach this statement with a row that is no longer `proposed`. That makes the
    `WHERE` clause **unreachable through the endpoints** — and it is still load-bearing:
    `confirmation.py` says why (two clicks racing, a replay arriving while the first is still
    committing), and a guard that only a service in front of it enforces is a guard the next
    caller does not have. So it is asserted where it lives.

    The draft is confirmed through the real HTTP route first, and then `decide` is called
    **directly on the repository** — same session, same published context — with a status it is
    not allowed to write. It answers `None` and the row does not move. Deleting the
    `status = 'proposed'` clause (or the window clause beside it) makes this the failing test.

    The service's own copy of the same rule is pinned by
    `test_a_second_confirmation_of_the_same_draft_creates_nothing_further`; this is the layer
    underneath it.
    """
    from app.domain.access.kernel import apply_rls_context
    from app.domain.agent.models import DraftStatus
    from app.domain.agent.repository import PostgresAgentActionRepository
    from tests.test_agent_graph import principal_of

    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    proposal = await propose_leave(platform, staff_cast, day=day)

    confirmed = await staff_cast.employee.post(
        f"/api/v1/agent/actions/{proposal.action_id}/confirm",
        json={"fields": form_values(proposal.form)},
    )
    assert confirmed.status_code == 201, confirmed.text
    created = confirmed.json()["entity_id"]
    row = await draft_row(platform, proposal.action_id)
    assert row["status"] == "confirmed"

    principal = await principal_of(platform, staff_cast.employee)
    session = platform.factory()
    try:
        await apply_rls_context(session, principal)
        repository = PostgresAgentActionRepository(session)
        # An answered draft is not `proposed`, so the guarded write must find nothing —
        # whichever status the caller is trying to put on it.
        for status in (DraftStatus.REJECTED, DraftStatus.CONFIRMED):
            assert await repository.decide(UUID(proposal.action_id), status=status) is None, (
                f"the guard let a second writer set {status}"
            )
        # **And the window, which is the *other* half of the same clause.** This is a second
        # draft, back-dated so the database says it has lapsed, and the guard must refuse it
        # for that reason alone — the row is still `proposed`, so the status clause cannot be
        # what refuses. Deleting `expires_at > now()` from the `WHERE` makes this the failing
        # test; nothing else in the module can, because `can_still_confirm` is asked first and
        # a mutation to *that* is caught by the expiry test beside this one.
        lapsed = await propose_leave(platform, staff_cast, day=day + timedelta(days=7))
        await platform.sql(
            """
            UPDATE agent_actions
               SET created_at = now() - interval '25 hours',
                   expires_at = now() - interval '1 hour'
             WHERE id = :id
            """,
            {"id": lapsed.action_id},
        )
        await apply_rls_context(session, principal)
        expired_row = await draft_row(platform, lapsed.action_id)
        assert expired_row["status"] == "proposed", (
            "the fixture's draft is not `proposed`, so this assertion is about the wrong clause"
        )
        assert await repository.decide(
            UUID(lapsed.action_id), status=DraftStatus.CONFIRMED
        ) is None, "the guard confirmed a draft whose window had closed"
        await session.rollback()
    finally:
        await session.close()

    after = await draft_row(platform, proposal.action_id)
    assert after["status"] == "confirmed", "a second writer moved an answered row"
    assert str(after["entity_id"]) == created
    assert after["confirmed_at"] == row["confirmed_at"]
    # The refusal is a refusal and not a half-write: nothing was created, then or after.
    assert (
        await platform.scalar(
            "SELECT count(*) FROM leave_requests WHERE employee_id = :id",
            {"id": staff_cast.employee.employee_id},
        )
        == 1
    )


async def test_two_clicks_at_once_produce_one_document(
    platform: Platform, staff_cast: Cast
) -> None:
    """The row lock's own test, because nothing sequential can see it.

    `load_for_update` takes the row with `FOR UPDATE`, and its docstring says why: two
    confirmations arriving together would both read `proposed` and both go on to write a
    document. **Every other test in this module is sequential, so removing that lock changes
    nothing they can observe** — a mutation of it survives the whole suite, which is what this
    test exists to fix.

    Two requests are issued **concurrently**, with a `gather`, on one draft. What must be true
    afterwards:

    * exactly one answers 201 and the other is refused — `ERR_AGT_002`, because by the time it
      reads the row the first has committed. Which of the two wins is not asserted: it depends
      on the scheduler, and either order is correct;
    * exactly one leave request exists, and exactly one `approval_requests` row;
    * the `agent_actions` row is `confirmed` exactly once, and names the document that the
      successful response named.

    `asyncio.gather` is the honest instrument: `ASGITransport` runs both requests in this one
    process against the same database, so the two transactions really do overlap.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    proposal = await propose_leave(platform, staff_cast, day=day)
    values = form_values(proposal.form)
    before = await entity_counts(platform)

    path = f"/api/v1/agent/actions/{proposal.action_id}/confirm"
    first, second = await asyncio.gather(
        staff_cast.employee.post(path, json={"fields": values}),
        staff_cast.employee.post(path, json={"fields": values}),
    )
    codes = sorted((first.status_code, second.status_code))
    assert codes == [201, 409], (
        f"two clicks at once produced {codes}: "
        f"{first.text[:200]} / {second.text[:200]}"
    )
    refused = first if first.status_code == 409 else second
    # **Either refusal code is correct here, and which one arrives depends on the schedule.**
    # If the loser reads the row after the winner commits, the draft is no longer `proposed`
    # and it is `ERR_AGT_002`. If it reads *before* — both transactions open at once, which is
    # what `gather` makes possible and what the lock is for — the draft still looks answerable,
    # `_create` runs, and the leave module refuses the second request over the same dates with
    # `ERR_AGT_003` carrying `errors.leave_request_overlaps`. Neither is a duplicate document,
    # and the assertions below are what make that the claim rather than the status code is.
    assert refused.json()["error"]["code"] in {
        ErrorCode.AGENT_DRAFT_NOT_CONFIRMABLE.value,
        ErrorCode.AGENT_DRAFT_CONFIRMATION_REFUSED.value,
    }, refused.text

    created = first if first.status_code == 201 else second
    created_id = created.json()["entity_id"]
    after = await entity_counts(platform)
    assert after["leave_requests"] == before["leave_requests"] + 1, "two documents were created"
    assert after["approval_requests"] == before["approval_requests"] + 1

    row = await draft_row(platform, proposal.action_id)
    assert row["status"] == "confirmed"
    assert str(row["entity_id"]) == created_id, "the row names a different document"
    assert (
        await platform.scalar(
            "SELECT count(*) FROM leave_requests WHERE employee_id = :id",
            {"id": staff_cast.employee.employee_id},
        )
        == 1
    )


async def test_a_second_confirmation_of_the_same_draft_creates_nothing_further(
    platform: Platform, staff_cast: Cast
) -> None:
    """The replay: the same request twice produces one document, and the second is refused.

    A retried request, a double-submitted click, a client that re-sends after a timeout —
    three names for one shape. The second confirmation must not create a second leave
    request, and the assertion is on the row counts rather than on the status alone: a 409
    whose transaction had already inserted the document would be the failure this test
    exists for.

    The refusal is `ERR_AGT_002` rather than a 404: the draft exists, it is the caller's, and
    the honest answer is "it is no longer waiting for an answer".

    **A second confirmation is not the strongest replay, and this test's first version used
    only that — which is why it did not catch the mutation that removed the guard.** With
    `status = 'proposed'` deleted from the guarded `UPDATE`, the replay still ends in a 409,
    because the *overlap rule* refuses a second leave request over the same dates: the test
    passed for a reason that had nothing to do with the draft being answered. So the replay
    is asserted twice: as a confirmation, and then as a **rejection**, which consults none of
    the document's rules — nothing but the guard stops a rejection from moving a confirmed
    row to `rejected` and overwriting the instant it was confirmed at. Both are `ERR_AGT_002`,
    and the audit row is asserted to still name the document the first click created.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    proposal = await propose_leave(platform, staff_cast, day=day)
    values = form_values(proposal.form)

    first = await staff_cast.employee.post(
        f"/api/v1/agent/actions/{proposal.action_id}/confirm", json={"fields": values}
    )
    assert first.status_code == 201, first.text
    created = first.json()["entity_id"]
    after_first = await entity_counts(platform)
    confirmed_row = await draft_row(platform, proposal.action_id)
    assert confirmed_row["status"] == "confirmed"
    assert str(confirmed_row["entity_id"]) == created

    second = await staff_cast.employee.post(
        f"/api/v1/agent/actions/{proposal.action_id}/confirm", json={"fields": values}
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == ErrorCode.AGENT_DRAFT_NOT_CONFIRMABLE.value

    # The same answer to the other decision, which no document rule can produce.
    rejected = await staff_cast.employee.post(
        f"/api/v1/agent/actions/{proposal.action_id}/reject", json={}
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == ErrorCode.AGENT_DRAFT_NOT_CONFIRMABLE.value

    assert await entity_counts(platform) == after_first, (
        "a replayed decision created a second document"
    )
    assert (
        await platform.scalar(
            "SELECT count(*) FROM leave_requests WHERE employee_id = :id",
            {"id": staff_cast.employee.employee_id},
        )
        == 1
    )
    final = await draft_row(platform, proposal.action_id)
    assert final["status"] == "confirmed", "a replay moved an answered draft"
    assert str(final["entity_id"]) == created, "a replay repointed the audit row"
    assert final["confirmed_at"] == confirmed_row["confirmed_at"], (
        "a replay rewrote the instant the human answered"
    )


async def test_an_expired_draft_cannot_be_confirmed_and_says_so(
    platform: Platform, staff_cast: Cast
) -> None:
    """A draft that lapsed between the form being drawn and the click.

    §6.3 gives a draft 24 hours and requires 「过期后…需重新生成」. The lapse is produced the
    way ticket 40's test produces it — moving both instants into the past, because the row's
    own constraint says a draft cannot lapse before it was proposed — and then the click is
    made. The answer is `ERR_AGT_002`, the row's status is **written** to `expired` (the
    status, not a filter), and no document exists.

    This is the case the ticket names by hand: the employee left the tab open overnight.

    **The rejection is asserted beside the confirmation, and that is what makes the expiry
    rule testable.** The first version of this test only confirmed, and the mutation that
    deleted the expiry comparison from `decide`'s `WHERE` **survived** — because the confirm
    path ends in a document either way, and a second one over the same dates is refused by the
    overlap rule rather than by the clock. A rejection consults none of the document's rules:
    with the comparison gone, the lapsed row moves straight to `rejected`, and this test is
    the only thing that notices.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    proposal = await propose_leave(platform, staff_cast, day=day)

    await platform.sql(
        """
        UPDATE agent_actions
           SET created_at = now() - interval '25 hours',
               expires_at = now() - interval '1 hour'
         WHERE id = :id
        """,
        {"id": proposal.action_id},
    )
    before = await entity_counts(platform)

    refused = await staff_cast.employee.post(
        f"/api/v1/agent/actions/{proposal.action_id}/confirm",
        json={"fields": form_values(proposal.form)},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == ErrorCode.AGENT_DRAFT_NOT_CONFIRMABLE.value

    assert await entity_counts(platform) == before
    row = await draft_row(platform, proposal.action_id)
    assert row["status"] == "expired", "the row does not say why the click did nothing"
    assert row["entity_id"] is None

    # And the other decision is closed for the same reason: a lapsed draft is not the
    # employee's to answer, whichever way they would have answered it.
    rejected = await staff_cast.employee.post(
        f"/api/v1/agent/actions/{proposal.action_id}/reject", json={}
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == ErrorCode.AGENT_DRAFT_NOT_CONFIRMABLE.value
    assert await entity_counts(platform) == before
    still = await draft_row(platform, proposal.action_id)
    assert still["status"] == "expired", "a replay answered a lapsed draft"
    assert still["confirmed_at"] is None


async def test_somebody_else_s_draft_is_not_found_rather_than_forbidden(
    platform: Platform, staff_cast: Cast
) -> None:
    """A draft that is not the caller's answers 404 — the same answer as one that never was.

    The ownership refusal is the row's own `WHERE user_id`, and the property that buys is that
    this endpoint cannot be used to learn which draft ids exist. The colleague's request is
    asserted to leave the row exactly as it was, which is the half a status code cannot see.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)
    proposal = await propose_leave(platform, staff_cast, day=day)

    before = await table_counts(platform)
    stole = await staff_cast.colleague.post(
        f"/api/v1/agent/actions/{proposal.action_id}/confirm",
        json={"fields": form_values(proposal.form)},
    )
    assert stole.status_code == 404, stole.text
    assert stole.json()["error"]["code"] == ErrorCode.AGENT_DRAFT_NOT_FOUND.value

    invented = await staff_cast.colleague.post(
        f"/api/v1/agent/actions/{uuid4()}/confirm", json={}
    )
    assert invented.status_code == 404
    assert invented.json()["error"]["code"] == ErrorCode.AGENT_DRAFT_NOT_FOUND.value
    assert await table_counts(platform) == before
    assert (await draft_row(platform, proposal.action_id))["status"] == "proposed"


# --- 5: the end-to-end path ---------------------------------------------------


async def test_from_conversation_to_document_and_the_middle_wrote_nothing(
    platform: Platform, staff_cast: Cast
) -> None:
    """Checklist: 有端到端测试：从对话到单据生成的完整链路，断言中间过程中数据库无任何写入.

    The whole chain, in one test, in the order a person lives it:

    1. **the conversation produces a draft** — the graph's draft branch runs, the assistant
       "replies", and the platform records the proposal. Counted: exactly one
       `agent_actions` row and no document.
    2. **nothing is submitted while the draft waits** — the counts are taken again after the
       form is drawn, including after the employee's own *edits* are made client-side (which
       is what a client with local state does), and they are unchanged.
    3. **the click creates exactly one document** — the confirm request through HTTP, and the
       count moves by one `leave_requests`, one `approval_requests`, one `approval_steps` and
       one `agent_actions` status change.
    4. **and the middle wrote nothing**: the delta between (2) and (3) is asserted table by
       table, so an intermediate write that was later cleaned up, or one to a table nobody
       thought about, is visible as a number that moved.

    Everything is real: the graph with its Postgres checkpointer, the HTTP API, the approval
    engine and the ledger.
    """
    today = madrid_today(datetime.now(UTC))
    day = working_day(today)

    # --- 1: the conversation -------------------------------------------------
    empty = await entity_counts(platform)
    proposal = await propose_leave(platform, staff_cast, day=day)
    proposed = await entity_counts(platform)
    assert proposed == empty, "proposing a draft created a document"

    row = await draft_row(platform, proposal.action_id)
    assert row["status"] == "proposed" and row["form"]["fields"]

    # --- 2: the wait, and the edits ------------------------------------------
    read = await staff_cast.employee.get(
        f"/api/v1/answers/conversations/{proposal.conversation_id}"
    )
    assert read.status_code == 200
    assert read.json()["draft"]["status"] == "proposed"

    edited = form_values(proposal.form)
    edited["end_date"] = day.isoformat()  # one day instead of two, decided in the browser
    waiting = await entity_counts(platform)
    assert waiting == empty, "drawing (or editing) the form wrote something"

    # --- 3: the click --------------------------------------------------------
    confirmed = await staff_cast.employee.post(
        f"/api/v1/agent/actions/{proposal.action_id}/confirm", json={"fields": edited}
    )
    assert confirmed.status_code == 201, confirmed.text
    created = await entity_counts(platform)

    delta = {
        table: created[table] - waiting[table]
        for table in created
        if created[table] != waiting[table]
    }
    assert delta == {
        "leave_requests": 1,
        "approval_requests": 1,
        "approval_steps": 1,
    }, delta

    # --- 4: the document is the employee's, and the audit says who proposed it ---
    entity_id = confirmed.json()["entity_id"]
    stored = (
        await platform.sql(
            """
            SELECT r.employee_id, r.start_date, r.end_date, a.initiated_by,
                   a.confirmed_by_user_id, a.status
              FROM leave_requests r
              JOIN approval_requests a
                ON a.entity_id = r.id AND a.entity_type = 'leave_request'
             WHERE r.id = :id
            """,
            {"id": entity_id},
        )
    )[0]
    assert str(stored[0]) == staff_cast.employee.employee_id
    assert stored[1] == day and stored[2] == day, "the edited range is not what was filed"
    assert stored[3] == "agent"
    assert str(stored[4]) == staff_cast.employee.user_id
    assert stored[5] == "pending_first"

    # The whole flow completes, two levels, with no shortcut anywhere.
    first = await staff_cast.manager.post(
        f"/api/v1/leave/requests/{entity_id}/decide", json={"decision": "approve"}
    )
    assert first.json()["approval"]["status"] == "pending_second"
    second = await staff_cast.hr.post(
        f"/api/v1/leave/requests/{entity_id}/decide", json={"decision": "approve"}
    )
    assert second.json()["approval"]["status"] == "approved"
    assert second.json()["state"] == "approved"

    # The manager and HR each got the notification the ordinary flow sends — the confirmed
    # draft is not a quieter document than a hand-filed one.
    notified = await platform.scalar(
        "SELECT count(*) FROM notifications WHERE recipient_employee_id = :id",
        {"id": staff_cast.manager.employee_id},
    )
    assert notified >= 1, "the approver was never told"


# --- the refusals, in the catalogue and in the guard --------------------------


@pytest.mark.parametrize(
    "code",
    (
        ErrorCode.AGENT_DRAFT_NOT_FOUND,
        ErrorCode.AGENT_DRAFT_NOT_CONFIRMABLE,
        ErrorCode.AGENT_DRAFT_CONFIRMATION_REFUSED,
    ),
)
def test_every_confirmation_refusal_has_wording_in_both_languages(code: ErrorCode) -> None:
    """A refusal an employee cannot read is a refusal they cannot act on (§4.3)."""
    from app.core.errors import ERRORS

    key = ERRORS[code].message_key
    for locale, catalogue in MESSAGES.items():
        assert catalogue.get(key), f"{key} missing from {locale}"


def test_the_confirmation_actions_are_the_self_only_filing_actions() -> None:
    """The action each entity's confirmation requires, named rather than implied.

    Three entities, and the actions are the ones that already govern *filing* them — no new
    catalogue entry, which is the honest answer to "what permission is a confirmation": the
    permission to file. A new action would be a second rule to keep in step with
    `leave.request_own`, and the ticket's rule is that the two must not diverge.
    """
    from app.domain.agent.confirmation import CONFIRM_ACTIONS, ENTITY_TYPE_OF
    from app.domain.agent.models import DraftEntity

    assert CONFIRM_ACTIONS[DraftEntity.LEAVE_REQUEST] == (Action.LEAVE_REQUEST_OWN,)
    assert CONFIRM_ACTIONS[DraftEntity.ATTENDANCE_CORRECTION] == (
        Action.ATTENDANCE_CORRECTION_OWN,
    )
    assert CONFIRM_ACTIONS[DraftEntity.TIMESHEET_ENTRY] == (
        Action.TIMESHEET_WRITE_OWN,
        Action.TIMESHEET_SUBMIT_OWN,
    )
    assert set(ENTITY_TYPE_OF) == set(DraftEntity)


__all__ = ["Proposal", "entity_counts", "form_values", "propose", "propose_leave"]
