"""The timesheet module: what a week holds, and when it stops being editable.

Five operations and one rule that shapes all of them: **a week is the employee's
until they file it, and the engine's decision afterwards**. `submit` hands the week
to the approval engine and writes back what the engine said; nothing in this module
decides whether a week is approved, because a second state machine beside the one
that already exists is the defect (`docs/architecture/codebase-design.md` §2.3).

Three decisions this module makes, each of them about *where* an answer comes from:

* **The over-budget warning is computed here, from the schedule.** Not from the
  client's total, and not from a copy of the expected hours stored beside the entry:
  `ScheduleService.day_expectations` is asked for the seven days of the week and each
  day's recorded total is compared with what that answered. It is returned by the
  read *and* by the submit response, because "the week you just filed contains a
  9-hour day against 8 expected" is the moment the employee can still act on it. It
  never refuses anything — see `domain/timesheet/models.py` for why the per-entry
  ceiling is 24 hours rather than the day's expected hours.

* **What an entry records comes from the project module.**
  `ProjectService.resolve_record_target` is the only thing that decides whether a
  target may be booked and whether it is billable, and it is the same call ticket 27's
  decision endpoint makes. `is_billable` is therefore never a request field. The same
  call refuses an inactive task and a project that is not `active`; the project's own
  dates are checked here against the target it returns, so "outside the project's
  dates" costs no second read.

* **Editable is derived, not merely stored.** The week's `status` column is what makes
  a week listable without a round trip per row, but the *decision* is the engine's:
  `_editable` reads `state_of` and reconciles the column with it, so a manager's
  rejection that went through the engine while this module was not looking still
  unlocks the week, and an approval still locks it. The column is a cache of the
  engine's answer and never a second opinion about it.

A rejected week is the employee's again, and a filed one is not. The engine's
rejection of one request is final *for that request* (`ApprovalService.submit` says
so), while this module's `rejected` is an invitation to correct and file again — so
`submit` files a **new** request for the same week rather than trying to reopen
anything, and the history of both attempts is the engine's, read back through
`state_of`, which already spans every round.

The service is the only thing that fetches rows, and it commits once per operation.
"""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction, record
from app.domain.access.principal import Principal
from app.domain.approval.models import ApprovalState, ApprovalStatus, SubmitContext
from app.domain.errors import DomainError
from app.domain.notification.approval import ApprovalNotifier
from app.domain.project.models import RecordTarget
from app.domain.project.repository import ProjectRepository
from app.domain.project.service import ProjectService
from app.domain.schedule.models import DayExpectation, ScheduleSource
from app.domain.schedule.service import ScheduleService
from app.domain.timesheet.errors import TimesheetErrorCode
from app.domain.timesheet.models import (
    DAYS_PER_WEEK,
    MAX_ENTRY_MINUTES,
    UNSET,
    DayTotal,
    EntryInput,
    EntryPatch,
    OverBudgetDay,
    ProjectLabel,
    Timesheet,
    TimesheetEntry,
    TimesheetPage,
    TimesheetStatus,
    WeekView,
    assert_monday,
)
from app.domain.timesheet.repository import TimesheetRepository

#: The entity type the approval engine files a week under. The engine stores it and
#: never interprets it; this module is the only reader.
ENTITY_TYPE = "timesheet"

#: How many entries one day may hold. A day is a finite thing and a grid row that
#: could hold a thousand entries is not a grid; the limit is stated so the refusal
#: names a number rather than being discovered as a timeout.
MAX_ENTRIES_PER_DAY = 50

#: What each engine status means for the week. Written as a table rather than as a
#: chain of `if`s at each site, so "which statuses are the employee's again" has one
#: answer for the gate, the read and the list.
_STATUS_OF: dict[ApprovalStatus, TimesheetStatus] = {
    ApprovalStatus.DRAFT: TimesheetStatus.REJECTED,
    ApprovalStatus.PENDING_FIRST: TimesheetStatus.PENDING,
    ApprovalStatus.PENDING_SECOND: TimesheetStatus.PENDING,
    ApprovalStatus.APPROVED: TimesheetStatus.APPROVED,
    ApprovalStatus.REJECTED: TimesheetStatus.REJECTED,
}


class TimesheetService:
    """Week reads, entry writes, and the filing that ends the editing.

    `principal` is the caller. It is required rather than optional because
    `resolve_record_target` asks the kernel with it: an entry's project has to be in
    *this person's* reach, and a service that could be built without one would be a
    service that could record time against anybody's project.
    """

    def __init__(
        self,
        repository: TimesheetRepository,
        session: AsyncSession,
        *,
        principal: Principal,
        projects: ProjectService,
        project_repository: ProjectRepository,
        expectations: ScheduleService,
        approvals: ApprovalNotifier,
        now=None,  # noqa: ANN001 - a TimeSource, as attendance/models defines it
    ) -> None:
        self._repository = repository
        self._session = session
        self._principal = principal
        self._projects = projects
        #: The same module's repository, for the two batch reads the grid's labels
        #: need. `ProjectService` is the *rules*; asking it for a task by id would be
        #: a rule where a lookup belongs.
        self._projects_repository = project_repository
        #: What the schedule expected. A collaborator rather than a repository, for
        #: the reason the attendance module records: this module is handed something
        #: that answers "what did the schedule expect" and never learns what a
        #: schedule is.
        self._expectations = expectations
        #: The engine *wrapped so the notifications cannot be forgotten*
        #: (`domain/notification/approval.py`). Constructing a bare `ApprovalService`
        #: here would still record decisions and silently lose the notices that were
        #: supposed to follow.
        self._approvals = approvals
        self._now = now or (lambda: datetime.now(UTC))

    @property
    def employee_id(self) -> UUID:
        """Whose timesheet this service is for: the caller and nobody else."""
        return self._principal.employee_id

    # --- reads --------------------------------------------------------------

    async def read_week(self, week_start: date) -> WeekView:
        """One week of the grid: seven days, their entries, and what was expected.

        **Creates nothing.** An unwritten week comes back as seven empty days with
        `timesheet=None` and a status of `draft`, which is what an employee sees
        before they have filled anything in. A `GET` that inserted the row would make
        every page load a write and would make "which weeks have I filed" a question
        the row's existence could no longer answer.
        """
        assert_monday(week_start)
        sheet = await self._reconciled(
            await self._repository.get_week(self.employee_id, week_start)
        )
        return await self._view(week_start, sheet)

    async def list_weeks(self, *, limit: int = 50, offset: int = 0) -> TimesheetPage:
        """The caller's own weeks, newest first.

        This is the 只能为本人填报 rule in its read form: the query is scoped to the
        caller's employee id and there is no parameter that could name somebody else.
        """
        return await self._repository.list_weeks(self.employee_id, limit=limit, offset=offset)

    async def status_of(self, week_start: date) -> ApprovalState | None:
        """The week's approval request, with every round's steps and decisions.

        Read from the engine rather than mirrored here: its decisions are
        append-only and authoritative, and a copy would be a second answer to "who
        rejected this, when, and what did they say". `ApprovalState` spans all
        rounds, which is exactly the 历史提交记录 the ticket asks to keep — a
        resubmission opens a new round and the earlier decisions stay readable.

        `None` for a week that has never been filed, which is an answer and not an
        error: the client shows 草稿 beside it.
        """
        assert_monday(week_start)
        sheet = await self._repository.get_week(self.employee_id, week_start)
        if sheet is None:
            return None
        return await self._approvals.state_of(ENTITY_TYPE, sheet.id)

    # --- entries ------------------------------------------------------------

    async def add_entry(
        self,
        week_start: date,
        *,
        entry_date: date,
        project_id: UUID,
        task_id: UUID,
        minutes: int,
        note: str | None = None,
    ) -> WeekView:
        """Write one entry into an editable week, and answer with the whole grid.

        The whole grid rather than the row, because every write moves a day total and
        the week total: a response carrying only the entry would leave the client to
        guess at the totals it has to display beside it.
        """
        assert_monday(week_start)
        self._require_day_in_week(entry_date, week_start)
        self._require_minutes(minutes)

        sheet = await self._editable(week_start)
        await self._require_room(week_start, entry_date)
        rules = await self._recordable(project_id, task_id, entry_date)

        await self._repository.add_entry(
            sheet.id,
            self.employee_id,
            week_start,
            EntryInput(
                entry_date=entry_date,
                project_id=project_id,
                task_id=task_id,
                minutes=minutes,
                is_billable=rules.is_billable,
                note=note,
            ),
        )
        await self._audit(
            AuditAction.TIMESHEET_ENTRY_WRITTEN,
            sheet.id,
            after={
                "employee_id": self.employee_id,
                "week_start": week_start,
                "entry_date": entry_date,
                "project_id": project_id,
                "task_id": task_id,
                "minutes": minutes,
                # The resolved value, not a claim: the trail records what the server
                # decided the entry was worth.
                "is_billable": rules.is_billable,
            },
        )
        await self._repository.commit()
        return await self.read_week(week_start)

    async def update_entry(
        self, week_start: date, entry_id: UUID, patch: EntryPatch
    ) -> WeekView:
        """Change one entry, and re-check everything the change touches.

        A patch may move an entry to another day, another project or another task, so
        every field that moves is validated by the same rule a write is: an update
        that could reach a state `add` would refuse is the half a test covering only
        `add` misses.
        """
        assert_monday(week_start)
        sheet = await self._editable(week_start)
        entry = await self._require_entry(sheet, entry_id)

        entry_date = patch.entry_date or entry.entry_date
        project_id = patch.project_id or entry.project_id
        task_id = patch.task_id or entry.task_id
        minutes = patch.minutes if patch.minutes is not None else entry.minutes
        self._require_day_in_week(entry_date, week_start)
        self._require_minutes(minutes)

        # The billable value is re-resolved only when the *target* moved. Re-resolving
        # it on a minutes-only edit would restate what a task was configured as on the
        # day the entry was written — the thing storing it is for.
        is_billable = entry.is_billable
        if (project_id, task_id) != (entry.project_id, entry.task_id):
            is_billable = (await self._recordable(project_id, task_id, entry_date)).is_billable

        cleaned = EntryPatch(
            entry_date=entry_date,
            project_id=project_id,
            task_id=task_id,
            minutes=minutes,
            # `UNSET` is "leave the note alone" and `None` is "clear it" — the two
            # requests a single nullable field cannot tell apart.
            note=patch.note,
        )
        await self._repository.update_entry(entry_id, cleaned, is_billable=is_billable)
        await self._audit(
            AuditAction.TIMESHEET_ENTRY_WRITTEN,
            sheet.id,
            before={
                "entry_id": entry_id,
                "entry_date": entry.entry_date,
                "project_id": entry.project_id,
                "task_id": entry.task_id,
                "minutes": entry.minutes,
                "is_billable": entry.is_billable,
            },
            after={
                "entry_id": entry_id,
                "entry_date": entry_date,
                "project_id": project_id,
                "task_id": task_id,
                "minutes": minutes,
                "is_billable": is_billable,
            },
            reason="entry updated",
        )
        await self._repository.commit()
        return await self.read_week(week_start)

    async def remove_entry(self, week_start: date, entry_id: UUID) -> WeekView:
        """Remove one entry from an editable week.

        Deletion rather than a paired negative entry: the 冲销 pair is ticket 29's
        supplementary-submission flow, and it exists because a *locked* week may not
        be rewritten. Inside a draft, a line somebody typed by mistake is a line that
        should not be there, and leaving a cancelling row would make every draft
        carry the history of its own typos.
        """
        assert_monday(week_start)
        sheet = await self._editable(week_start)
        entry = await self._require_entry(sheet, entry_id)

        await self._repository.delete_entry(entry_id)
        await self._audit(
            AuditAction.TIMESHEET_ENTRY_REMOVED,
            sheet.id,
            before={
                "entry_id": entry_id,
                "entry_date": entry.entry_date,
                "project_id": entry.project_id,
                "task_id": entry.task_id,
                "minutes": entry.minutes,
            },
        )
        await self._repository.commit()
        return await self.read_week(week_start)

    async def copy_previous_week(self, week_start: date) -> WeekView:
        """Fill an empty draft week with a copy of the one before it.

        **Entries only, never the status.** The source week's status is a statement
        about *that* week — it was filed, or approved — and copying it would make the
        target claim a decision nobody took on it. What is copied is the shape of the
        week: days, projects, tasks, minutes, notes, and the billable value that was
        resolved at the time, so a later change to a task's configuration cannot
        silently restate what was copied.

        **Refused when the target is not editable, and when it is not empty.** The
        first is the rule every other write obeys. The second is the one this
        operation could get wrong quietly: a copy that *added* to existing entries
        would leave the employee with a duplicated Monday and no way to see which row
        came from where, and "copy" does not say "merge".
        """
        assert_monday(week_start)
        target = await self._editable(week_start)
        source_start = week_start - timedelta(days=DAYS_PER_WEEK)

        existing = await self._repository.entries_in_week(self.employee_id, week_start)
        if existing:
            raise DomainError(
                TimesheetErrorCode.TIMESHEET_COPY_TARGET_NOT_EMPTY,
                detail=(
                    f"week {week_start} already holds {len(existing)} entries; a copy "
                    "fills an empty week rather than merging into one"
                ),
            )

        source = await self._repository.entries_in_week(self.employee_id, source_start)
        if not source:
            # Refused rather than answered with an unchanged week: "copied nothing"
            # and "there was nothing to copy" are different answers, and only one of
            # them tells the employee to fill the week in by hand.
            raise DomainError(
                TimesheetErrorCode.TIMESHEET_COPY_SOURCE_INVALID,
                detail=f"the week of {source_start} has no entries to copy",
            )

        for entry in source:
            await self._repository.add_entry(
                target.id,
                self.employee_id,
                week_start,
                EntryInput(
                    entry_date=entry.entry_date + timedelta(days=DAYS_PER_WEEK),
                    project_id=entry.project_id,
                    task_id=entry.task_id,
                    minutes=entry.minutes,
                    is_billable=entry.is_billable,
                    note=entry.note,
                ),
            )
        await self._audit(
            AuditAction.TIMESHEET_COPIED,
            target.id,
            after={
                "employee_id": self.employee_id,
                "week_start": week_start,
                "source_week_start": source_start,
                "entries": len(source),
            },
        )
        await self._repository.commit()
        return await self.read_week(week_start)

    # --- filing -------------------------------------------------------------

    async def submit(self, week_start: date) -> WeekView:
        """File the week with the approval engine, and answer with the warning.

        The engine decides who approves — the direct manager, then HR — so this
        method tests no role and resolves no route. What it does decide is whether the
        week is *fileable*: a draft or a rejected week is, and anything already filed
        or decided is not, with the catalogued code the ticket asks for.

        The caller is the week's owner by construction (`principal`, in the
        constructor), and `_editable` is what refuses a week that is not the
        employee's own any more. Filing somebody else's week cannot be expressed
        through this service at all; the route refuses it first, with the 403 the
        ticket names.
        """
        assert_monday(week_start)
        sheet = await self._editable(week_start)

        entries = await self._repository.entries_in_week(self.employee_id, week_start)
        if not entries:
            raise DomainError(
                TimesheetErrorCode.TIMESHEET_NOT_EDITABLE,
                detail=f"week {week_start} has no entries; there is nothing to file",
            )
        await self._require_still_recordable(entries)

        try:
            request_id = await self._approvals.submit(
                ENTITY_TYPE, sheet.id, self.employee_id, SubmitContext()
            )
        except DomainError as error:
            # The engine's refusal, in this module's vocabulary: the client routes on
            # the code, and `ERR_APR_002` would tell an employee nothing about the
            # week in front of them. The engine's own code travels in the detail.
            raise DomainError(
                TimesheetErrorCode.TIMESHEET_SUBMISSION_REFUSED,
                detail=f"the approval engine refused week {week_start}: {error}",
            ) from error

        await self._repository.set_status(
            sheet.id,
            TimesheetStatus.PENDING,
            approval_request_id=request_id,
            submitted_at=self._now(),
        )
        await self._audit(
            AuditAction.TIMESHEET_SUBMITTED,
            sheet.id,
            after={
                "employee_id": self.employee_id,
                "week_start": week_start,
                "approval_request_id": request_id,
                "total_minutes": sum(entry.minutes for entry in entries),
                # The warning travels into the trail as well: a week filed with a
                # 9-hour day is the fact an approver is being asked about, and the
                # record of what the employee was told is worth keeping.
                "over_budget_days": [
                    day.entry_date.isoformat()
                    for day in _over_budget(entries, await self._expected(week_start))
                ],
            },
        )
        await self._repository.commit()
        # Read back rather than assembled: the totals and the over-budget warning are
        # computed in one place, so a submission and the read that follows it cannot
        # report different numbers.
        return await self.read_week(week_start)

    # --- internals ----------------------------------------------------------

    async def _view(self, week_start: date, sheet: Timesheet | None) -> WeekView:
        """Assemble the grid: entries by day, the schedule's answer, the totals.

        One pass over the rows and one call for the seven days' expectations, rather
        than a query per day: a grid is seven cells, and the naive shape of this
        method is seven round trips for one screen.
        """
        entries = await self._repository.entries_in_week(self.employee_id, week_start)
        expectations = await self._expected(week_start)

        by_date: dict[date, list[TimesheetEntry]] = {
            week_start + timedelta(days=offset): [] for offset in range(DAYS_PER_WEEK)
        }
        for entry in entries:
            by_date.setdefault(entry.entry_date, []).append(entry)

        days: list[DayTotal] = []
        for offset in range(DAYS_PER_WEEK):
            day = week_start + timedelta(days=offset)
            rows = tuple(by_date.get(day, ()))
            expectations_for_day = expectations.get(day)
            days.append(
                DayTotal(
                    entry_date=day,
                    weekday=offset,
                    entries=rows,
                    total_minutes=sum(row.minutes for row in rows),
                    expected_minutes=_expected_minutes(expectations_for_day),
                    expectation_source=_source(expectations_for_day),
                    is_holiday=expectations_for_day is not None
                    and expectations_for_day.is_holiday,
                )
            )

        week = tuple(days)
        expected_total = (
            None
            if all(day.expected_minutes is None for day in week)
            else sum(day.expected_minutes or 0 for day in week)
        )
        return WeekView(
            employee_id=self.employee_id,
            week_start=week_start,
            days=week,
            timesheet=sheet,
            entries_total_minutes=sum(day.total_minutes for day in week),
            expected_total_minutes=expected_total,
            over_budget_days=_over_budget(entries, expectations),
        )

    async def _expected(self, week_start: date) -> dict[date, DayExpectation]:
        return await self._expectations.day_expectations(
            self.employee_id, week_start, week_start + timedelta(days=DAYS_PER_WEEK - 1)
        )

    async def labels_for(
        self, entries: list[TimesheetEntry]
    ) -> dict[UUID, ProjectLabel]:
        """What each entry's project and task are called, for one grid.

        The grid shows a cell's task, not its uuid, so the read needs the names — and
        it needs them in two statements rather than two per entry, which is what a
        seven-day screen with a dozen cells would otherwise cost. Ticket 27's two
        batch reads exist for this and nothing else.
        """
        if not entries:
            return {}
        tasks = await self._projects_repository.tasks_by_ids(
            {entry.task_id for entry in entries}
        )
        projects = await self._projects_repository.projects_by_ids(
            {entry.project_id for entry in entries}
        )
        labels: dict[UUID, ProjectLabel] = {}
        for entry in entries:
            task = tasks.get(entry.task_id)
            project = projects.get(entry.project_id)
            labels[entry.task_id] = ProjectLabel(
                project_id=entry.project_id,
                task_id=entry.task_id,
                project_code=None if project is None else project.code,
                task_code=None if task is None else task.code,
                task_name_es=None if task is None else task.name_es,
                task_name_en=None if task is None else task.name_en,
            )
        return labels

    async def _reconciled(self, sheet: Timesheet | None) -> Timesheet | None:
        """The week's status as the engine has it, written back when it disagrees.

        The column exists so a list of weeks does not cost a round trip per row, and a
        cache only ever written by this module's own `submit` would go stale the moment
        a manager decided — which is the one moment it matters. The read reconciles it,
        so `approved` starts showing on the very next request rather than after the
        next write.
        """
        if sheet is None:
            return None
        state = await self._approvals.state_of(ENTITY_TYPE, sheet.id)
        if state is None:
            return sheet
        status = _STATUS_OF.get(state.status)
        if status is None or status is sheet.status:
            return sheet
        # Not committed here: a read that found a stale cache repairs it in the
        # transaction it already owns, and the caller's commit ends it. A *refusal*
        # commits what it repaired before it refuses — see `_editable`.
        return await self._repository.set_status(sheet.id, status)

    async def _editable(self, week_start: date) -> Timesheet:
        """The week's row, when this employee may still write in it.

        Two refusals in one place: the week has to exist or be creatable (the first
        write of a week creates it), and it has to be in a status the employee owns. The
        status is read through `_reconciled`, so the engine's latest decision is what
        decides — a week the manager just approved is locked on the next keystroke, not
        after a cache expiry.

        **A refusal commits the correction it just made.** Otherwise the one write path
        that discovers a stale cache is also the one that throws the repair away with
        its own rollback, and the row stays wrong until somebody's read happens to
        succeed — which, for a locked week, is never.
        """
        await self._require_employee()
        sheet = await self._repository.get_week(self.employee_id, week_start)
        if sheet is None:
            sheet = await self._create(week_start)
        else:
            refreshed = await self._reconciled(sheet)
            if refreshed is not None and refreshed.status is not sheet.status:
                await self._repository.commit()
            sheet = refreshed or sheet
        self._require_editable(sheet)
        return sheet

    async def _create(self, week_start: date) -> Timesheet:
        try:
            sheet = await self._repository.create_week(self.employee_id, week_start)
        except IntegrityError as error:
            # A second request in flight for the same week. The unique constraint is
            # what makes this a fact rather than a race, and the answer is the row the
            # other request wrote.
            await self._repository.rollback()
            existing = await self._repository.get_week(self.employee_id, week_start)
            if existing is not None:
                return existing
            raise DomainError(
                TimesheetErrorCode.TIMESHEET_ALREADY_EXISTS,
                detail=f"week {week_start} for employee {self.employee_id}: {error}",
            ) from error
        await self._audit(
            AuditAction.TIMESHEET_CREATED,
            sheet.id,
            after={
                "employee_id": self.employee_id,
                "week_start": week_start,
                "status": sheet.status,
            },
        )
        # Not committed here: the caller's operation commits once, so the week and
        # the entry that caused it to exist land together.
        return sheet

    async def _recordable(
        self, project_id: UUID, task_id: UUID, entry_date: date
    ) -> RecordTarget:
        """What this entry would record, or a catalogued refusal.

        The reach check is the project module's, asked with the caller's principal:
        an entry's project has to be one *this person* may book against, and the
        service does not re-derive that from departments and managers itself.
        """
        target = await self._projects.resolve_record_target(
            self._principal, project_id, task_id
        )
        if not target.project.is_recordable:
            # `resolve_record_target` already refuses a non-active project through the
            # kernel's status clause; this branch names the *status*, because "draft"
            # and "archived" are different answers with different remedies.
            raise DomainError(
                TimesheetErrorCode.TIMESHEET_ENTRY_PROJECT_NOT_RECORDABLE,
                detail=(
                    f"project {target.project.code} is {target.project.status}; only an "
                    "active project accepts new time"
                ),
            )
        if not _covers(target, entry_date):
            raise DomainError(
                TimesheetErrorCode.TIMESHEET_ENTRY_OUTSIDE_PROJECT_DATES,
                detail=(
                    f"{entry_date} is outside project {target.project.code}'s dates "
                    f"({target.project.start_date}..{target.project.end_date or 'open'})"
                ),
            )
        return target

    async def _require_still_recordable(self, entries: list[TimesheetEntry]) -> None:
        """Re-check every entry's target before the week leaves the employee's hands.

        A project can be archived, or a task switched off, between the Monday somebody
        typed the entry and the Friday they filed it. Filing a week that names a task
        nobody may record against would hand the approver a document that could not
        have been written that day — and the refusal now is one the employee can still
        act on, which the same refusal after approval is not.
        """
        for entry in entries:
            await self._recordable(entry.project_id, entry.task_id, entry.entry_date)

    async def _require_employee(self) -> None:
        if not await self._repository.employee_exists(self.employee_id):
            raise DomainError(
                TimesheetErrorCode.TIMESHEET_NOT_FOUND,
                detail=f"unknown employee {self.employee_id}",
            )

    async def _require_entry(self, sheet: Timesheet, entry_id: UUID) -> TimesheetEntry:
        entry = await self._repository.get_entry(entry_id)
        if entry is None or entry.timesheet_id != sheet.id:
            # The same answer for "no such entry" and "that entry is another week's":
            # telling them apart would make this an existence oracle over the whole
            # table, and the route already names the week.
            raise DomainError(
                TimesheetErrorCode.TIMESHEET_ENTRY_NOT_FOUND,
                detail=f"no entry {entry_id} in timesheet {sheet.id}",
            )
        return entry

    async def _require_room(self, week_start: date, entry_date: date) -> None:
        entries = await self._repository.entries_in_week(self.employee_id, week_start)
        on_day = sum(1 for entry in entries if entry.entry_date == entry_date)
        if on_day >= MAX_ENTRIES_PER_DAY:
            raise DomainError(
                TimesheetErrorCode.INVALID_REQUEST,
                detail=(
                    f"{entry_date} already holds {on_day} entries; a day holds at most "
                    f"{MAX_ENTRIES_PER_DAY}"
                ),
            )

    @staticmethod
    def _require_editable(sheet: Timesheet) -> None:
        if not sheet.is_editable:
            raise DomainError(
                TimesheetErrorCode.TIMESHEET_NOT_EDITABLE,
                detail=(
                    f"timesheet {sheet.id} is {sheet.status}; a filed week is read-only "
                    "until a decision returns it"
                ),
            )

    @staticmethod
    def _require_minutes(minutes: int) -> None:
        if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0:
            raise DomainError(
                TimesheetErrorCode.TIMESHEET_ENTRY_MINUTES_INVALID,
                detail=f"minutes must be a positive integer, not {minutes!r}",
            )
        if minutes > MAX_ENTRY_MINUTES:
            raise DomainError(
                TimesheetErrorCode.TIMESHEET_ENTRY_MINUTES_INVALID,
                detail=(
                    f"{minutes} minutes is longer than a day; the ceiling is "
                    f"{MAX_ENTRY_MINUTES} (24 h), so a typo is refused and a long day "
                    "is not"
                ),
            )

    @staticmethod
    def _require_day_in_week(entry_date: date, week_start: date) -> None:
        if not week_start <= entry_date < week_start + timedelta(days=DAYS_PER_WEEK):
            raise DomainError(
                TimesheetErrorCode.INVALID_REQUEST,
                detail=(
                    f"{entry_date} is not in the week of {week_start}; a timesheet week "
                    "runs Monday to Sunday"
                ),
            )

    async def _audit(
        self,
        action: AuditAction,
        sheet_id: UUID,
        *,
        before: dict | None = None,
        after: dict | None = None,
        reason: str | None = None,
    ) -> None:
        await record(
            self._session,
            action=action,
            entity_type=ENTITY_TYPE,
            entity_id=sheet_id,
            before=before,
            after=after,
            reason=reason,
        )


def _covers(target: RecordTarget, entry_date: date) -> bool:
    """Whether the project's own dates admit this day. `None` end means open."""
    if entry_date < target.project.start_date:
        return False
    end = target.project.end_date
    return end is None or entry_date <= end


def _expected_minutes(expectation: DayExpectation | None) -> int | None:
    """The day's figure, or `None` when no schedule reaches this person.

    `ScheduleSource.NONE` means nobody has configured a week for them, which is the
    absence of a decision rather than a decision to expect nothing — the distinction
    `schedule.models.DayExpectation` draws, and the reason the grid can say "no
    schedule configured" instead of showing a confident zero.
    """
    if expectation is None or expectation.source is ScheduleSource.NONE:
        return None
    return expectation.expected_minutes


def _source(expectation: DayExpectation | None) -> str | None:
    return None if expectation is None else str(expectation.source)


def _over_budget(
    entries: list[TimesheetEntry], expectations: dict[date, DayExpectation]
) -> list[OverBudgetDay]:
    """Every day whose recorded minutes exceed what the schedule expected.

    Computed from the rows and the schedule, from nothing the client sent. A day
    nobody was expected to work counts: a holiday or a rest day is expected to hold
    nothing, so an entry on one is over by every minute of it — and excluding those
    days would quietly drop the case where the warning matters most.
    """
    totals: dict[date, int] = {}
    for entry in entries:
        totals[entry.entry_date] = totals.get(entry.entry_date, 0) + entry.minutes

    over: list[OverBudgetDay] = []
    for day in sorted(totals):
        expected = _expected_minutes(expectations.get(day))
        if expected is None:
            continue
        total = totals[day]
        if total > expected:
            over.append(
                OverBudgetDay(
                    entry_date=day,
                    total_minutes=total,
                    expected_minutes=expected,
                    over_minutes=total - expected,
                )
            )
    return over


__all__ = ["ENTITY_TYPE", "MAX_ENTRIES_PER_DAY", "TimesheetService", "UNSET"]
