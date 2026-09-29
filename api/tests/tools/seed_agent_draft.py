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
from app.domain.answer.models import title_for
from app.domain.answer.repository import PostgresAnswerRepository
from app.domain.attendance.business_day import MADRID, madrid_today
from app.domain.project.models import ProjectQuery
from app.domain.project.service import ProjectService
from app.domain.schedule.service import ScheduleService
from app.domain.timesheet.models import monday_of
from app.models.account import User
from app.models.agent_action import AgentAction
from app.models.answer import RagConversation
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

#: Two more, one per decision the confirmation point offers (ticket 41).
#:
#: **They exist because confirming is destructive to a fixture.** `checkDraftForm` draws each
#: of the three drafts above and asserts its fields are editable and that the form is the
#: assistant's; the confirmation check then *answers* one of them, which moves its
#: `agent_actions` row out of `proposed` and creates a real leave request. Reusing the three
#: would make the two checks interfere — and would make the check's own re-run fail, because
#: a `confirmed` draft has no confirm button. So the decisions get drafts of their own, and
#: each run of this script writes them fresh (it clears the conversations it wrote first).
CONFIRM_QUESTION = "Confirmame el permiso de la semana que viene"
REJECT_QUESTION = "Prepara un permiso que voy a descartar"

#: How far back the *decision* drafts are searched for a free week, in days.
#:
#: **They need a week of their own, and finding one is a search rather than an offset.**
#: Confirming a leave draft creates a leave request, and the module refuses a second one that
#: overlaps a live one (`ERR_LVE_008`) — so a re-run of this fixture against a database where
#: the confirmation check has already run would be refused for a collision it caused itself.
#: A fixed offset is what the first two versions used, and it broke on the *third* run of the
#: check, because a still-live request from an earlier run had reached that week by then. So
#: the fixture asks the database which weekdays in the last couple of months are free of live
#: leave and picks one, and `EAM_DRAFT_DECISION_SEARCH_DAYS` widens the search if a deployment
#: has filled that window.
DECISION_SEARCH_DAYS = int(os.environ.get("EAM_DRAFT_DECISION_SEARCH_DAYS", "60"))

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
        # **The context is published before the search, and that is not a formality.** The
        # request's permission context is `set_config(..., is_local => true)`, transaction-
        # scoped, and `leave_requests` is row-level-secured: a query without it matches **no
        # rows**, so a search for "a week with no live leave" would find every week free and
        # hand back a day the draft tool then refuses. The fixture's own second run found
        # exactly that, and the error message named the wrong cause.
        await apply_rls_context(session, principal)
        # **The expired draft needs a *different* day from the leave draft**, and it did not
        # have one until the fourth run of the visual check. The fixture writes four
        # `draft_leave_request` drafts and only the decision pair are confirmed — but the
        # *second run* of this script in one database is refused anyway, because the expired
        # draft reuses the leave draft's dates and the **previous run's live draft request** is
        # still there. That is the fixture refusing itself; the days are searched for instead,
        # the same way the correction's quiet day is.
        expired_day = await _free_weekday(
            session, principal.employee_id, start=working - timedelta(days=7)
        )
        quiet = await _quiet_day(
            session, principal.employee_id, start=working - timedelta(days=1)
        )
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
                    f"{name} was refused (fields: {arguments}). The causes a fixture meets are: "
                    "seed_timesheet_demo.py has not run (the schedule and the project); a live "
                    "leave request of this account covers the leave draft's week — including "
                    "one a previous run of visual-check.mjs created by confirming a draft; or "
                    "the day chosen for a punch correction already holds punches. Withdraw the "
                    "leave from the leave screen, or re-run scripts/demo/seed-screens.ps1, "
                    "which resets this account's year."
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

        await _expired(session, principal, expired_day)
        await _decisions(session, principal, working)


async def _decisions(session, principal, working: date) -> None:  # noqa: ANN001
    """The two drafts the confirmation check answers: one to confirm, one to discard.

    Filed under the same tool as the first draft above and in a week of their own (see
    `DECISION_SEARCH_DAYS`), in conversations of their own — the interface shows the
    conversation's *newest* draft, so one thread per decision is the only shape in which two
    of them can be on screen independently.
    """
    day = await _free_weekday(session, principal.employee_id, start=working)
    arguments = {
        "leave_type": "annual",
        "start_date": day.isoformat(),
        "end_date": (day + timedelta(days=1)).isoformat(),
    }
    for question in (CONFIRM_QUESTION, REJECT_QUESTION):
        await _publish(session, principal)
        form = await _draft(session, principal, "draft_leave_request", arguments)
        if form is None:
            raise SystemExit(
                f"{question!r} was refused: has seed_timesheet_demo.py run, and is the "
                f"week {day} free of live leave? "
                "EAM_DRAFT_DECISION_SEARCH_DAYS widens the search."
            )
        service = service_for(session, ttl_hours=get_settings().agent_draft_ttl_hours)
        recorded = await service.record_draft(
            principal=principal,
            conversation_id=None,
            question=question,
            tool_name="draft_leave_request",
            tool_input=arguments,
            tool_output=form.as_dict(),
            form=form,
        )
        print(f"draft: decision -> conversation {recorded.conversation_id}")


async def _free_weekday(session, employee_id, *, start: date) -> date:  # noqa: ANN001
    """A past weekday whose two-day range collides with no live leave of this employee.

    **Why this is a query rather than an offset.** Confirming a leave draft writes a real
    leave request, and the module refuses a second one over a live request — so a fixture
    that always used "the Monday of last week" was refused by its own previous run's document
    the second time the visual check ran, and the third time it was refused for a request an
    *earlier* version had left two weeks back. The set of taken days is a fact about the
    database, so the fixture reads it: the same shape as `_quiet_day` and `_working_day`
    above, and the same reason — a fixture that assumes a free week is a fixture that fails
    with a message that looks like a product defect.

    Walked back from `start` and skipping weekends, because a range with no working day in it
    is refused by the module itself.
    """
    day = start
    for _ in range(DECISION_SEARCH_DAYS):
        if day.weekday() < 5:
            taken = await session.scalar(
                text(
                    """
                    SELECT count(*)
                      FROM leave_requests
                     WHERE employee_id = :employee_id
                       AND withdrawn_at IS NULL
                       AND start_date <= :end_date
                       AND end_date >= :start_date
                       AND (approval_request_id IS NULL
                            OR approval_request_id NOT IN
                               (SELECT id FROM approval_requests WHERE status = 'rejected'))
                    """
                ),
                {
                    "employee_id": employee_id,
                    "start_date": day,
                    "end_date": day + timedelta(days=1),
                },
            )
            if not taken:
                return day
        day -= timedelta(days=1)
    raise SystemExit(
        f"no free weekday in the last {DECISION_SEARCH_DAYS} days for the decision drafts; "
        "set EAM_DRAFT_DECISION_SEARCH_DAYS higher"
    )


async def _expired(session, principal, day: date) -> None:  # noqa: ANN001
    """One more draft, moved 25 hours into the past: the state the interface has to draw.

    Both instants move, not only the expiry: the row's own `ck_agent_actions_expiry` says a
    draft cannot lapse before it was proposed, and a fixture that broke its own constraint
    would be a fixture contradicting the schema it is seeding for.

    `day` is a day of its own rather than the leave draft's — see `main`: reusing the leave
    draft's dates made the *second* run of this script refuse itself, because the first run's
    draft request is still live over them.
    """
    await _publish(session, principal)
    arguments = {
        "leave_type": "annual",
        "start_date": day.isoformat(),
        "end_date": (day + timedelta(days=1)).isoformat(),
    }
    form = await _draft(session, principal, "draft_leave_request", arguments)
    if form is None:
        raise SystemExit(
            f"the expired fixture was refused for {day}..{day + timedelta(days=1)}: that week "
            "is not free. EAM_DRAFT_DECISION_SEARCH_DAYS widens the search."
        )
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
    """A day this account is expected to work in the given week, **with no live leave over it**.

    Asked of the schedule module rather than assumed: the leave tool prices a range in
    *working* days, and a fixture that drafted a Saturday would be refused for a reason that
    looks like a product defect.

    The second half — the leave question — was added after the visual check's own
    confirmation caught this fixture out: confirming one of the decision drafts writes a real
    leave request for the current week, and the *next* run's leave draft was refused by the
    document the previous run had created. The schedule answers "is this a day somebody
    works"; the database answers "is this a day this person is not already away", and both
    have to be true before a leave draft over it means anything.
    """
    expectations = ScheduleService(PostgresScheduleRepository(session), session)
    for offset in range(7):
        day = week + timedelta(days=offset)
        expected = await expectations.day_expectations(employee_id, day, day)
        if expected.get(day) is None or not expected[day].expected_minutes:
            continue
        overlapping = await session.scalar(
            text(
                """
                SELECT count(*)
                  FROM leave_requests
                 WHERE employee_id = :employee_id
                   AND withdrawn_at IS NULL
                   AND start_date <= :end_date
                   AND end_date >= :start_date
                   AND (approval_request_id IS NULL
                        OR approval_request_id NOT IN
                           (SELECT id FROM approval_requests WHERE status = 'rejected'))
                """
            ),
            {"employee_id": employee_id, "start_date": day, "end_date": day + timedelta(days=1)},
        )
        if not overlapping:
            return day
    raise SystemExit(
        "no working day in that week is free of this account's live leave: run "
        "seed_timesheet_demo.py first (it creates the company schedule these drafts are "
        "checked against), and withdraw whatever this fixture's earlier runs confirmed"
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
    # The decision drafts too, found by the *titles* their questions produced rather than by
    # their tool: they are the same tool as the first three, so a tool-shaped filter would
    # take the form checks' own fixture with them. `title_for` is the same function the answer
    # path derives a conversation's title with, so this asks the product's own question.
    decisions = {title_for(question) for question in (CONFIRM_QUESTION, REJECT_QUESTION)}
    conversations |= set(
        await session.scalars(
            select(AgentAction.conversation_id).where(
                AgentAction.user_id == user_id,
                AgentAction.conversation_id.in_(
                    select(RagConversation.id).where(
                        RagConversation.user_id == user_id,
                        RagConversation.title.in_(sorted(decisions)),
                    )
                ),
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
