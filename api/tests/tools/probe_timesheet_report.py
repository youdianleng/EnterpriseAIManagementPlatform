"""Measure the hours report at seed scale, and say whether it needs an index.

Ticket 30 asks for the report's responsiveness to be *measured and recorded* rather
than asserted, and this is the measurement. It answers three questions, in the order
they have to be asked:

1. **How long is the report query at a realistic volume?** A hundred employees is what
   the platform is seeded with (M0), and a quarter is the period a report is usually
   asked for; the probe generates half a year of it, corrections included, so the
   numbers are for the shape a finance or HR reader actually asks for rather than for
   a hand-picked small case.
2. **Is the planner choosing the right thing?** `EXPLAIN (ANALYZE, BUFFERS)` on the
   grouped statement, printed rather than summarised: at this scale the honest answer
   may well be "sequential scan", and that is a fact about the volume rather than a
   defect — the same reading `docs/DESIGN.md` §10.3 records for the vector index.
3. **Does it need an index?** Decided from the two above, and the decision is recorded
   in the ticket and in §10.7 of the design.

**It refuses to run against the development database.** Everything it writes is
tagged with a probe code and deleted afterwards, but a probe that quietly seeds
twenty thousand rows into `eam` is a probe somebody runs once and regrets; so
`TEST_DATABASE_NAME` must name something other than `eam`, which is also what the
ticket's isolation recipe sets.

    docker compose exec -e TEST_DATABASE_NAME=eam_test_rep -T api \
        python /app/tests/tools/probe_timesheet_report.py

The database has to exist and be migrated: running the suite once with the same
`TEST_DATABASE_NAME` does both.
"""

import asyncio
import statistics
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

# Probes and seeders are run as scripts; the app package lives one level up.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.domain.access.kernel import ResourceKind, filter_for
from app.domain.access.principal import Principal
from app.domain.timesheet.models import monday_of
from app.domain.timesheet.report import ReportDimension, ReportFilter
from app.repositories.timesheet import (
    PostgresTimesheetRepository,
    _report_aggregates,
    _report_from,
    _report_keys,
    _report_where,
)

#: The company the probe invents: the M0 seed is six departments and a hundred
#: employees, and a quarter is the period a timesheet report is asked for. Half a year
#: is used rather than a quarter so the measurement is at the upper end of the range a
#: reader asks for, which is where an index would start to matter.
DEPARTMENTS = 6
EMPLOYEES = 100
PROJECTS = 25
WEEKS = 26
ENTRIES_PER_WEEK = 5
#: One week in five is corrected by a supplement: a reversal and a replacement, which
#: is what makes the aggregate's two-sided sums exercise both signs.
CORRECTION_EVERY = 5
REPLACEMENT_MINUTES = 300

MINUTES_PER_ENTRY = 480
#: Long enough to be a real report and short enough that the probe is not itself the
#: slow part when somebody re-runs it.
ITERATIONS = 30

PREFIX = "probe30"


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile, in milliseconds.

    Nearest-rank rather than an interpolation: with thirty samples an interpolated p95
    is a number between two measurements, and the point of a recorded figure is that it
    is a measurement.
    """
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(fraction * len(ordered) + 0.5)) - 1))
    return ordered[index]


def _report(values: list[float]) -> str:
    return (
        f"p50 {statistics.median(values):7.2f} ms   "
        f"p95 {_percentile(values, 0.95):7.2f} ms   "
        f"min {min(values):7.2f} ms   max {max(values):7.2f} ms"
    )


async def _seed(session) -> dict:
    """Write the company, and return the ids the measurements need."""
    suffix = uuid4().hex[:8]
    department_ids = [uuid4() for _ in range(DEPARTMENTS)]
    employee_ids = [uuid4() for _ in range(EMPLOYEES)]
    project_ids = [uuid4() for _ in range(PROJECTS)]
    task_ids = [uuid4() for _ in range(PROJECTS * 2)]

    for index, department_id in enumerate(department_ids):
        await session.execute(
            text(
                """
                INSERT INTO departments (id, code, name_es, name_en, path, depth,
                                        clearance_level, is_active)
                VALUES (:id, :code, :name, :name, :path, 1, 'low', true)
                """
            ),
            {
                "id": department_id,
                "code": f"{PREFIX}{suffix}d{index}",
                "name": f"Probe {index}",
                "path": f"probe{index}",
            },
        )
    for index, employee_id in enumerate(employee_ids):
        await session.execute(
            text(
                """
                INSERT INTO employees (id, first_name, last_name, email, hire_date, status)
                VALUES (:id, 'Probe', :last, :email, '2020-01-01', 'active')
                """
            ),
            {
                "id": employee_id,
                "last": f"Employee{index}",
                "email": f"{PREFIX}{suffix}{index}@probe.es",
            },
        )
    for index, project_id in enumerate(project_ids):
        await session.execute(
            text(
                """
                INSERT INTO projects (id, code, name_es, name_en, department_id,
                                      manager_employee_id, is_billable_default, status,
                                      start_date, end_date)
                VALUES (:id, :code, :name, :name, :department, :manager, true, 'active',
                        '2019-01-01', NULL)
                """
            ),
            {
                "id": project_id,
                "code": f"{PREFIX}{suffix}p{index}",
                "name": f"Probe project {index}",
                "department": department_ids[index % DEPARTMENTS],
                "manager": employee_ids[index % EMPLOYEES],
            },
        )
    for offset, task_id in enumerate(task_ids):
        project_id = project_ids[offset // 2]
        await session.execute(
            text(
                """
                INSERT INTO project_tasks (id, project_id, code, name_es, name_en,
                                           is_billable, is_active)
                VALUES (:id, :project, :code, :name, :name, :billable, true)
                """
            ),
            {
                "id": task_id,
                "project": project_id,
                "code": f"t{offset}",
                "name": f"Probe task {offset}",
                # Half the tasks unbillable, so both subtotals carry weight and the
                # aggregate cannot be a single `sum(minutes)`.
                "billable": offset % 2 == 0,
            },
        )
    await session.commit()
    return {
        "departments": department_ids,
        "employees": employee_ids,
        "projects": project_ids,
        "tasks": task_ids,
        "suffix": suffix,
    }


async def _seed_weeks(session, ids: dict, weeks: list) -> None:
    """One approved week per employee per Monday, with a correction in one week in five.

    Entries are written into a `draft` sheet and the sheet is approved afterwards,
    because `time_entries_guard_week_lock` refuses an entry in an approved one — the
    backstop ticket 29 installed working exactly as intended, which is why the probe
    goes round it rather than through it.
    """
    employees = ids["employees"]
    projects = ids["projects"]
    tasks = ids["tasks"]
    for week_index, week in enumerate(weeks):
        for employee_index, employee_id in enumerate(employees):
            project_id = projects[(employee_index + week_index) % len(projects)]
            task_id = tasks[((employee_index + week_index) % len(projects)) * 2]
            sheet_id = uuid4()
            await session.execute(
                text(
                    """
                    INSERT INTO timesheets (id, employee_id, week_start, status,
                                            is_supplementary)
                    VALUES (:id, :employee, :week, 'draft', false)
                    """
                ),
                {"id": sheet_id, "employee": employee_id, "week": week},
            )
            first_entry = uuid4()
            # The first entry's own fields, kept so the correction below is the
            # negation the trigger demands: same day, project, task and billable flag.
            first_billable = employee_index % 2 == 0
            for day in range(ENTRIES_PER_WEEK):
                await session.execute(
                    text(
                        """
                        INSERT INTO timesheet_entries
                            (id, timesheet_id, employee_id, week_start, entry_date,
                             project_id, task_id, minutes, is_billable, entry_type)
                        VALUES (:id, :sheet, :employee, :week, :day, :project, :task,
                                :minutes, :billable, 'normal')
                        """
                    ),
                    {
                        "id": first_entry if day == 0 else uuid4(),
                        "sheet": sheet_id,
                        "employee": employee_id,
                        "week": week,
                        "day": week + timedelta(days=day),
                        "project": project_id,
                        "task": task_id,
                        "minutes": MINUTES_PER_ENTRY,
                        "billable": (employee_index + day) % 2 == 0,
                    },
                )
            await session.execute(
                text("UPDATE timesheets SET status = 'approved' WHERE id = :id"),
                {"id": sheet_id},
            )

            if week_index % CORRECTION_EVERY != 0:
                continue
            # A correction, approved: the reversal and the replacement are two rows in
            # a sheet of their own, and both count once that sheet is approved.
            supplement_id = uuid4()
            await session.execute(
                text(
                    """
                    INSERT INTO timesheets (id, employee_id, week_start, status,
                                            is_supplementary, supersedes_timesheet_id)
                    VALUES (:id, :employee, :week, 'draft', true, :original)
                    """
                ),
                {
                    "id": supplement_id,
                    "employee": employee_id,
                    "week": week,
                    "original": sheet_id,
                },
            )
            for minutes, entry_type, reverses, billable in (
                (-MINUTES_PER_ENTRY, "reversal", first_entry, first_billable),
                (REPLACEMENT_MINUTES, "normal", None, first_billable),
            ):
                await session.execute(
                    text(
                        """
                        INSERT INTO timesheet_entries
                            (id, timesheet_id, employee_id, week_start, entry_date,
                             project_id, task_id, minutes, is_billable, entry_type,
                             reverses_entry_id)
                        VALUES (:id, :sheet, :employee, :week, :week, :project, :task,
                                :minutes, :billable, :entry_type, :reverses)
                        """
                    ),
                    {
                        "id": uuid4(),
                        "sheet": supplement_id,
                        "employee": employee_id,
                        "week": week,
                        "project": project_id,
                        "task": task_id,
                        "minutes": minutes,
                        "billable": billable,
                        "entry_type": entry_type,
                        "reverses": reverses,
                    },
                )
            await session.execute(
                text("UPDATE timesheets SET status = 'approved' WHERE id = :id"),
                {"id": supplement_id},
            )
        await session.commit()


def _principal(ids: dict, roles: set[str], reports: int = 0) -> Principal:
    """A caller of the shape being measured: HR, a manager, or a project manager."""
    employee_id = ids["employees"][0]
    return Principal(
        user_id=uuid4(),
        employee_id=employee_id,
        username="probe",
        roles=frozenset(roles | {"employee"}),
        clearance_level="low",
        department_ids=frozenset(ids["departments"]),
        primary_department_id=ids["departments"][0],
        is_manager="manager" in roles,
        reports_employee_ids=frozenset(ids["employees"][1 : 1 + reports]),
    )


async def _measure(session, repository, label: str, principal: Principal, query: ReportFilter):
    """Time the two statements the report endpoint issues, and print the plan."""
    spec = filter_for(principal, ResourceKind.TIMESHEET_REPORT)
    rows = await repository.report_rows(spec, query)
    totals = await repository.report_totals(spec, query)

    row_times: list[float] = []
    total_times: list[float] = []
    for _ in range(ITERATIONS):
        started = time.perf_counter()
        await repository.report_rows(spec, query)
        row_times.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        await repository.report_totals(spec, query)
        total_times.append((time.perf_counter() - started) * 1000)

    print(f"\n== {label} ==")
    print(f"   rows      {_report(row_times)}   ({len(rows)} rows stated)")
    print(f"   totals    {_report(total_times)}")
    print(
        f"   together  {_report([a + b for a, b in zip(row_times, total_times, strict=True)])}"
        f"   total={totals.total_minutes} min  billable={totals.billable_minutes}"
    )
    return row_times + total_times


async def _clear_probe_rows(session) -> None:
    """Remove anything a previous run left, by this probe's own codes.

    An interrupted run leaves an approved company behind and nothing else can tell it
    from real data, so the probe clears its own rows before it seeds and after it
    measures. Unlocking the sheets first is not a formality: `time_entries_guard_week_lock`
    refuses the cascade's delete just as readily as it refuses a writer, which this
    probe discovered the honest way.
    """
    statements = (
        "UPDATE timesheets SET status = 'draft' WHERE employee_id IN "
        "(SELECT id FROM employees WHERE email LIKE :pattern)",
        "DELETE FROM timesheet_entries WHERE employee_id IN "
        "(SELECT id FROM employees WHERE email LIKE :pattern)",
        "DELETE FROM timesheets WHERE employee_id IN "
        "(SELECT id FROM employees WHERE email LIKE :pattern)",
        "DELETE FROM project_tasks WHERE project_id IN "
        "(SELECT id FROM projects WHERE code LIKE :pattern)",
        "DELETE FROM projects WHERE code LIKE :pattern",
        "DELETE FROM employees WHERE email LIKE :pattern",
        "DELETE FROM departments WHERE code LIKE :pattern",
    )
    for statement in statements:
        await session.execute(text(statement), {"pattern": f"{PREFIX}%"})
    await session.commit()


async def main() -> None:
    settings = get_settings()
    if settings.test_database_name in {"eam", ""}:
        raise SystemExit(
            "refusing to run against the development database: set TEST_DATABASE_NAME "
            "to a scratch database (see the module docstring)"
        )
    print(f"database: {settings.test_database_name}")

    engine = create_async_engine(settings.test_database_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    ids: dict = {}
    try:
        async with factory() as session:
            await _clear_probe_rows(session)
            ids = await _seed(session)
            # Half a year of Mondays, ending with the week that has just finished: the
            # probe's period is a period somebody could ask for, not a synthetic one.
            last_week = monday_of(date.today()) - timedelta(weeks=1)
            weeks = [last_week - timedelta(weeks=offset) for offset in reversed(range(WEEKS))]
            print(
                f"seeding {EMPLOYEES} employees x {WEEKS} weeks x {ENTRIES_PER_WEEK} entries "
                f"with a correction every {CORRECTION_EVERY}th week ..."
            )
            started = time.perf_counter()
            await _seed_weeks(session, ids, weeks)
            print(f"seeded in {time.perf_counter() - started:.1f} s")

            counts = (
                await session.execute(
                    text(
                        """
                        SELECT (SELECT count(*) FROM timesheet_entries) AS entries,
                               (SELECT count(*) FROM timesheets) AS sheets,
                               (SELECT count(*) FROM timesheet_entries e
                                  JOIN timesheets t ON t.id = e.timesheet_id
                                 WHERE t.status = 'approved') AS approved_entries
                        """
                    )
                )
            ).one()
            print(
                f"volume: {counts.entries} entries over {counts.sheets} sheets, "
                f"{counts.approved_entries} of them in approved sheets"
            )
            last_week = weeks[-1]

            repository = PostgresTimesheetRepository(session)
            period = {"from_date": weeks[0], "to_date": last_week}
            hr = ReportFilter(**period, group_by=(ReportDimension.PROJECT,))
            manager = ReportFilter(**period, group_by=(ReportDimension.EMPLOYEE,))
            by_week = ReportFilter(**period, group_by=(ReportDimension.PERIOD,))
            two_dimensions = ReportFilter(
                **period, group_by=(ReportDimension.DEPARTMENT, ReportDimension.PROJECT)
            )
            one_employee = ReportFilter(
                **period,
                group_by=(ReportDimension.PERIOD,),
                employee_ids=(ids["employees"][0],),
            )

            all_times: list[float] = []
            all_times += await _measure(
                session, repository, "HR, by project (the whole company)",
                _principal(ids, {"hr"}), hr,
            )
            all_times += await _measure(
                session, repository, "manager, by employee (ten reports)",
                _principal(ids, {"manager"}, reports=10), manager,
            )
            all_times += await _measure(
                session, repository, "project manager, by period (their projects)",
                _principal(ids, {"manager"}), by_week,
            )
            all_times += await _measure(
                session, repository, "HR, by department and project",
                _principal(ids, {"hr"}), two_dimensions,
            )
            all_times += await _measure(
                session, repository, "HR, one employee by period",
                _principal(ids, {"hr"}), one_employee,
            )
            print(f"\n== every statement together ==\n   {_report(all_times)}")

            print("\n== the grouped statement, as the planner runs it ==")
            for label, principal, query in (
                ("HR, by project (no reach predicate)", _principal(ids, {"hr"}), hr),
                (
                    "project manager, by period (the project clause)",
                    _principal(ids, {"manager"}),
                    by_week,
                ),
            ):
                await _explain(session, label, principal, query)
            await session.rollback()
    finally:
        if ids:
            async with factory() as session:
                await _clear_probe_rows(session)
                print("\nprobe rows removed")
        await engine.dispose()


async def _explain(session, label: str, principal: Principal, query: ReportFilter) -> None:
    """`EXPLAIN (ANALYZE, BUFFERS)` of one report statement, printed in full.

    Printed rather than summarised because the plan is the evidence for the index
    decision: whether the reach predicate rides an existing index or is a filter over
    a sequential scan is what says whether a migration is justified.
    """
    spec = filter_for(principal, ResourceKind.TIMESHEET_REPORT)
    keys = [
        column.label(f"k{index}") for index, column in enumerate(_report_keys(query.group_by))
    ]
    statement = (
        _report_from([*keys, *_report_aggregates()], query.group_by)
        .where(*_report_where(spec, query))
        .group_by(*keys)
        .order_by(*keys)
    )
    print(f"\n-- {label} --")
    plan = await session.execute(
        text(
            "EXPLAIN (ANALYZE, BUFFERS) "
            + str(statement.compile(compile_kwargs={"literal_binds": True}))
        )
    )
    for line in plan.scalars():
        print("   " + line)


if __name__ == "__main__":
    asyncio.run(main())
