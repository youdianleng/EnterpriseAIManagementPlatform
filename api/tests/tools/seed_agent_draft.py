"""Seed three draft forms for the demo account, so the interface has something to draw.

Ticket 40's form is shown where the conversation is, and a draft only exists after the
assistant produced one. The graph's draft branch takes its call from the state (a model's
function call arrives there in ticket 42 and a caller names one today), so the fixture names
it the same way a test does — and **everything else is the product's own code**: the tools
validate, the `PrefillForm` is the tool's, and the row is written by
`AgentActionService.record_draft`, which is the platform write the node performs.

Three drafts, one conversation each, because the interface shows the *newest* draft of the
conversation it has open — three cards in one thread would be a picture of a state this
system cannot produce.

Idempotent: it removes the drafts and conversations an earlier run left (identified by the
questions below) and writes them again. Run it after `seed_timesheet_demo.py`, which is what
provides the company week and the project the drafts below are checked against:

    docker compose exec -T api python /app/tests/tools/seed_timesheet_demo.py
    docker compose exec -T api python /app/tests/tools/seed_agent_draft.py
"""

import asyncio
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

# Probes and seeders are run as scripts; the app package lives one level up.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import func, select, text

from app.ai.tools import ToolCall, ToolContext, invoke
from app.config import get_settings
from app.db import get_session_factory
from app.domain.access.kernel import apply_rls_context
from app.domain.access.snapshot import resolve_principal
from app.domain.agent.service import service_for
from app.domain.answer.repository import PostgresAnswerRepository
from app.domain.attendance.business_day import MADRID, madrid_today
from app.domain.project.models import ProjectQuery
from app.domain.project.service import ProjectService
from app.domain.schedule.service import ScheduleService
from app.domain.timesheet.models import monday_of
from app.models.account import User
from app.models.agent_action import AgentAction
from app.models.attendance import AttendanceEvent
from app.repositories.project import PostgresProjectRepository
from app.repositories.schedule import PostgresScheduleRepository

#: The account `web/scripts/visual-check.mjs` signs in as.
TARGET = os.environ.get("FIXTURE_USERNAME", "devlead")

#: The three questions the drafts are filed under. Fixed strings, because the visual check
#: finds the conversation by its title — `answer.models.title_for` derives it from the
#: question, and a fixture whose title moved would be a check that silently stopped looking.
QUESTIONS = {
    "draft_leave_request": "Quiero pedir dos dias de vacaciones",
    "draft_attendance_correction": "Olvide fichar la salida el lunes",
    "draft_timesheet": "Apuntame ocho horas el lunes en el proyecto",
}

#: A fourth draft, back-dated past its 24 hours. The interface has to draw the *expired*
#: state — §6.3 requires the employee to be told to regenerate — and the only honest way to
#: produce one is to write a draft and let the database's clock say it is old.
EXPIRED_QUESTION = "Quiero pedir un dia de vacaciones"
EXPIRED_HOURS_AGO = 25

MINUTES = 480


async def main() -> None:
    factory = get_session_factory()
    async with factory() as session:
        user_id = await session.scalar(select(User.id).where(User.username == TARGET))
        if user_id is None:
            raise SystemExit(f"no account {TARGET!r}: run `python -m app.seed` first")
        principal = await resolve_principal(session, user_id)
        if principal is None:
            raise SystemExit(f"no permission snapshot for {TARGET!r}")

        week = monday_of(madrid_today(datetime.now(UTC)))
        working = await _working_day(session, principal.employee_id, week)
        quiet = await _quiet_day(session, principal.employee_id, start=working)
        project, task = await _project(session, principal)
        await _publish(session, principal)
        await _clear(session, user_id)

        answers = {
            "draft_leave_request": {
                "leave_type": "annual",
                "start_date": working.isoformat(),
                "end_date": (working + timedelta(days=1)).isoformat(),
            },
            "draft_attendance_correction": {
                "business_date": quiet.isoformat(),
                "kind": "clock_out",
                "corrected_at": datetime(
                    quiet.year, quiet.month, quiet.day, 16, 10, tzinfo=MADRID
                ).isoformat(),
                "reason": "Olvide fichar la salida al terminar la jornada.",
            },
            "draft_timesheet": {
                "week_start": week.isoformat(),
                "entry_date": working.isoformat(),
                "project_id": project.id,
                "task_id": task.id,
                "minutes": MINUTES,
                "note": "Revision de la interfaz",
            },
        }

        for name, arguments in answers.items():
            # Published per iteration, not once: the request context is
            # `set_config(..., is_local => true)`, which is transaction-scoped, and
            # `record_draft` commits — so the next conversation's insert is a *new*
            # transaction and the database's own policy would refuse it without this. The
            # fixture therefore runs under exactly the policies a request runs under, which
            # is also what makes it evidence that the drafts it writes are a caller's own.
            await apply_rls_context(session, principal)
            form = await _draft(session, principal, name, arguments)
            if form is None:
                raise SystemExit(
                    f"{name} was refused: the fixture's arguments are not valid for "
                    f"{TARGET} — has seed_timesheet_demo.py run?"
                )
            service = service_for(
                session, ttl_hours=get_settings().agent_draft_ttl_hours
            )
            recorded = await service.record_draft(
                principal=principal,
                conversation_id=None,
                question=QUESTIONS[name],
                tool_name=name,
                tool_input=arguments,
                tool_output=form.as_dict(),
                form=form,
            )
            print(
                f"draft: {name} -> conversation {recorded.conversation_id} "
                f"(expires {recorded.action.expires_at.isoformat()})"
            )

        await _expired(session, principal, working)


async def _expired(session, principal, working: date) -> None:  # noqa: ANN001
    """One more draft, moved 25 hours into the past: the state the interface has to draw.

    Both instants move, not only the expiry: the row's own `ck_agent_actions_expiry` says a
    draft cannot lapse before it was proposed, and a fixture that broke its own constraint
    would be a fixture contradicting the schema it is seeding for.
    """
    await _publish(session, principal)
    arguments = {
        "leave_type": "annual",
        "start_date": working.isoformat(),
        "end_date": (working + timedelta(days=1)).isoformat(),
    }
    form = await _draft(session, principal, "draft_leave_request", arguments)
    assert form is not None, "the expired fixture's arguments are not valid"
    service = service_for(session, ttl_hours=get_settings().agent_draft_ttl_hours)
    recorded = await service.record_draft(
        principal=principal,
        conversation_id=None,
        question=EXPIRED_QUESTION,
        tool_name="draft_leave_request",
        tool_input=arguments,
        tool_output=form.as_dict(),
        form=form,
    )
    # Republished: `record_draft` commits, and the request context is transaction-scoped.
    await _publish(session, principal)
    await session.execute(
        text(
            """
            UPDATE agent_actions
               SET created_at = now() - make_interval(hours => :proposed),
                   expires_at = now() - make_interval(hours => :lapsed)
             WHERE id = :id
            """
        ),
        {
            "proposed": EXPIRED_HOURS_AGO,
            "lapsed": EXPIRED_HOURS_AGO - 1,
            "id": recorded.action.id,
        },
    )
    await session.commit()
    print(f"draft: expired -> conversation {recorded.conversation_id}")


async def _draft(session, principal, name: str, arguments: dict):  # noqa: ANN001
    """Run one draft tool as the demo account, exactly as the graph's node would."""
    result = await invoke(
        ToolCall(name=name, arguments=arguments),
        ToolContext(
            principal=principal,
            session=session,
            today=madrid_today(datetime.now(UTC)),
        ),
    )
    if str(result.outcome) != "ok":
        print(f"  {name} refused: {result.data.get('message_key')}")
        return None
    from app.domain.agent.models import PrefillForm

    return PrefillForm.from_stored(result.data)


async def _working_day(session, employee_id, week: date) -> date:  # noqa: ANN001
    """A day this account is expected to work in the given week.

    Asked of the schedule module rather than assumed: the leave tool prices a range in
    *working* days, and a fixture that drafted a Saturday would be refused for a reason that
    looks like a product defect.
    """
    expectations = ScheduleService(PostgresScheduleRepository(session), session)
    for offset in range(7):
        day = week + timedelta(days=offset)
        expected = await expectations.day_expectations(employee_id, day, day)
        if expected.get(day) is not None and expected[day].expected_minutes:
            return day
    raise SystemExit(
        "no working day in that week: run seed_timesheet_demo.py first, which creates the "
        "company schedule these drafts are checked against"
    )


async def _quiet_day(session, employee_id, *, start: date) -> date:  # noqa: ANN001
    """A working day this account has **no punch** on, walking back from `start`.

    Chosen from the stream rather than assumed, because `scripts/demo/seed-screens.ps1` fills
    a month of shifts: a correction of a day that already holds two clock-outs is one the flow
    *refuses to resolve* (`ERR_ATT_010`, and rightly so), which would make this fixture fail
    with a message that looks like a product defect. A day with nothing on it is also the
    case a correction is most often about — the shift somebody forgot to clock out of.
    """
    day = start
    for _ in range(40):
        punches = await session.scalar(
            select(func.count())
            .select_from(AttendanceEvent)
            .where(
                AttendanceEvent.employee_id == employee_id,
                AttendanceEvent.business_date == day,
            )
        )
        if not punches:
            return day
        day -= timedelta(days=1)
        while day.weekday() >= 5:
            day -= timedelta(days=1)
    raise SystemExit("no punch-free working day in the last forty days of demo data")


async def _project(session, principal):  # noqa: ANN001
    """A project and a task this account may book, from the kernel's own answer."""
    service = ProjectService(PostgresProjectRepository(session), session)
    page = await service.recordable_projects(principal, ProjectQuery(limit=1))
    assert page.items, (
        "no project this account may record time against: run seed_timesheet_demo.py first"
    )
    project = page.items[0]
    tasks = await service.list_tasks(project.id, include_inactive=False)
    assert tasks, f"project {project.code} has no active task"
    return project, tasks[0]


async def _clear(session, user_id: UUID) -> None:  # noqa: ANN001
    """Remove what an earlier run wrote, through the product's own removal.

    `PostgresAnswerRepository.delete_for` is what `DELETE /answers/conversations/{id}` calls:
    the flag §3.6 gives a conversation's owner, so the earlier run's thread leaves the sidebar
    immediately and the fixture does not need a privilege the request role deliberately does
    not have — `eam_app` holds no `DELETE` on a conversation at all (ticket 34's
    `REVOKE DELETE`), which this script discovered the honest way. Its `agent_actions` rows
    stay: they are the audit of what was proposed, and an audit is not something a fixture
    may erase.

    The drafts are selected through `agent_actions` — the ones naming this script's tools —
    rather than by removing every conversation the account has: a demo account's other
    conversations are somebody's earlier browsing.
    """
    repository = PostgresAnswerRepository(session)
    conversations = set(
        await session.scalars(
            select(AgentAction.conversation_id).where(
                AgentAction.user_id == user_id,
                AgentAction.tool_name.in_(sorted(QUESTIONS)),
            )
        )
    )
    removed = 0
    for conversation_id in conversations:
        if await repository.delete_for(user_id, conversation_id):
            removed += 1
    if removed:
        print(f"cleared {removed} conversation(s) from earlier runs")
    await repository.commit()


async def _publish(session, principal) -> None:  # noqa: ANN001
    """Publish the caller's permission context, as a request does.

    Without it every statement against a row-level-secured table matches **no rows** — the
    documented failure mode of a missing context, and one this script met the honest way: the
    first version's cleanup deleted nothing and its back-dating update changed nothing, both
    silently. `agent_actions` is secured by the same rule as the conversation it belongs to,
    so this is not a test that forgets to be a test: it is the fixture proving it runs under
    the policies a request runs under.
    """
    await apply_rls_context(session, principal)


if __name__ == "__main__":
    asyncio.run(main())
