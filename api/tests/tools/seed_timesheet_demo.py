"""Seed the development database with what the timesheet-flow check needs.

Two rows, written through the modules that own them so nothing is invented here:

* a **company default schedule** expecting eight hours, Monday to Friday — the
  expectation the over-budget warning is measured against, which is what makes
  "nine hours against eight" a fact the server can compute;
* an **active project with an active task**, owned by the department the demo account
  signs in for and managed by that account — because `filter_for` reaches a project
  through the caller's departments or by their having been named its manager, and the
  fixture's whole purpose is a project the demo account may actually book against.

`FIJO_TARGET` names the account, defaulting to `devlead`, the one
`web/scripts/visual-check.mjs` and `web/scripts/timesheet-data-check.mjs` sign in as.

Idempotent: it reports what it found and writes only what is missing. Run it with

    docker compose exec -T api python /app/tests/tools/seed_timesheet_demo.py
"""

import asyncio
import os
import sys
from datetime import date, time, timedelta
from pathlib import Path

# Probes and seeders are run as scripts; the app package lives one level up.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import delete, func, select

from app.db import get_session_factory
from app.domain.project.models import (
    ProjectInput,
    ProjectPatch,
    ProjectStatus,
    ProjectTaskInput,
)
from app.domain.schedule.models import ScheduleDayInput, ScheduleInput
from app.domain.schedule.service import ScheduleService
from app.models.account import User
from app.models.employee import EmployeeAssignment
from app.models.timesheet import Timesheet, TimesheetEntry
from app.repositories.project import PostgresProjectRepository
from app.repositories.schedule import PostgresScheduleRepository

EXPECTED_MINUTES = 480
WORKDAYS = (0, 1, 2, 3, 4)
PROJECT_CODE = "FIJO-2026"
TASK_CODE = "01"

#: The account whose department the project belongs to, and which manages it.
FIJO_TARGET = os.environ.get("FIXTURE_USERNAME", "devlead")


async def main() -> None:
    factory = get_session_factory()
    async with factory() as session:
        await _schedule(session)
        employee_id, department_id = await _target(session)
        await _project(session, employee_id, department_id)
        await _reset_week(session, employee_id)
        await session.commit()


async def _reset_week(session, employee_id) -> None:  # noqa: ANN001
    """Clear the week the browser checks fill in, so they can be run more than once.

    A filed week is read-only, so a second run of `timesheet-data-check.mjs` would be
    refused before it wrote anything. This puts the demo database back where the check
    expects to start.

    **The approval history is deliberately left alone.** Deleting it is refused by
    PostgreSQL — `approval_decisions` grants INSERT and SELECT and nothing else (migration
    0009), which is the append-only guarantee that lets a decision be read as evidence,
    and this script discovered that the honest way. So the *week* is removed and its
    request stays: the record of what was filed and what a manager said survives, and the
    next write for that week starts a fresh one.
    """
    week = _fixture_week()
    sheet = await session.scalar(
        select(Timesheet).where(
            Timesheet.employee_id == employee_id, Timesheet.week_start == week
        )
    )
    if sheet is None:
        print(f"week: {week} has never been written, so there is nothing to reset")
        return

    entries = await session.scalar(
        select(func.count())
        .select_from(TimesheetEntry)
        .where(TimesheetEntry.timesheet_id == sheet.id)
    )
    request_id = sheet.approval_request_id
    # The week's row goes, not merely its contents. A returned request leaves the engine's
    # request in `draft`, and `ApprovalService.submit` reads that as "an open request" and
    # opens a *new round* on it rather than a fresh filing — so an emptied week would come
    # back still pointing at a request the engine never forgot. The entries go with the
    # row through `fk_timesheet_entries_week`.
    await session.execute(delete(Timesheet).where(Timesheet.id == sheet.id))
    print(
        f"week: removed {week} ({entries} entries); the next write creates it afresh"
    )
    if request_id is not None:
        # Named rather than deleted: `approval_decisions` takes INSERT and SELECT and
        # nothing else, which is the append-only guarantee the decisions are read as
        # evidence under, and a fixture has no business asking PostgreSQL to relax it.
        print(
            "week: its earlier approval request and decisions are kept as history "
            f"(request {request_id})"
        )


def _fixture_week() -> date:
    """Three weeks back, which is where `timesheet-data-check.mjs` fills its week in."""
    today = date.today()
    return today - timedelta(days=today.weekday() + 21)


async def _target(session) -> tuple[object, object]:
    """The demo account's employee and department: where the project has to sit."""
    row = (
        await session.execute(
            select(User.employee_id, EmployeeAssignment.department_id)
            .join(EmployeeAssignment, EmployeeAssignment.employee_id == User.employee_id)
            .where(User.username == FIJO_TARGET, EmployeeAssignment.end_date.is_(None))
            .order_by(EmployeeAssignment.is_primary.desc())
            .limit(1)
        )
    ).first()
    assert row is not None, f"no active assignment for {FIJO_TARGET}: seed the database first"
    print(f"fixture: {FIJO_TARGET} works in department {row[1]}")
    return row[0], row[1]


async def _schedule(session) -> None:
    """A company default week of eight-hour days, if there is not one already."""
    service = ScheduleService(PostgresScheduleRepository(session), session)
    for existing in await service.list_schedules(include_inactive=False):
        if existing.is_default and all(
            existing.minutes_on(weekday) == EXPECTED_MINUTES for weekday in WORKDAYS
        ):
            print(f"schedule: {existing.code} already expects 8 h x 5 days")
            return

    created = await service.create_schedule(
        ScheduleInput(
            code="ESTANDAR-8H",
            name_es="Jornada estándar",
            name_en="Standard week",
            is_default=True,
            days=tuple(
                ScheduleDayInput(
                    weekday=weekday,
                    expected_minutes=EXPECTED_MINUTES,
                    start_time=time(8, 0),
                    end_time=time(16, 0),
                )
                for weekday in WORKDAYS
            ),
        )
    )
    print(f"schedule: created {created.code} (8 h x 5 days, the company default)")


async def _project(session, employee_id, department_id) -> None:  # noqa: ANN001
    """An active project with one task, in the demo account's department.

    An existing project is *moved* to that department and manager rather than left where
    it is. The whole point of the fixture is a project the demo account may book against,
    and reach is decided by the department and the named manager — a project sitting in
    somebody else's department is a fixture that produces a 422 halfway through the check
    it exists to enable.
    """
    repository = PostgresProjectRepository(session)
    existing = await repository.find_by_code(PROJECT_CODE)
    if existing is not None:
        await repository.update(
            existing.id,
            ProjectPatch(
                status=ProjectStatus.ACTIVE,
                department_id=department_id,
                start_date=min(existing.start_date, date(2020, 1, 1)),
            ),
        )
        if existing.manager_employee_id != employee_id:
            await repository.reassign_manager(existing.id, employee_id)
        print(
            f"project: {existing.code} is active in the demo department, "
            f"managed by {FIJO_TARGET}"
        )
        return

    project = await repository.save(
        ProjectInput(
            code=PROJECT_CODE,
            name_es="Proyecto fijo",
            name_en="Standing project",
            department_id=department_id,
            manager_employee_id=employee_id,
            start_date=date(2020, 1, 1),
            status=ProjectStatus.ACTIVE,
        ),
        created_by_employee_id=employee_id,
    )
    task = await repository.save_task(
        project.id,
        ProjectTaskInput(code=TASK_CODE, name_es="Trabajo", name_en="Work"),
    )
    print(f"project: created {project.code} ({project.status}) with task {task.code}")


if __name__ == "__main__":
    asyncio.run(main())

