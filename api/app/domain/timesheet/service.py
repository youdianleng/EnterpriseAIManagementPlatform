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

Ticket 29 adds the lock, the correction and the window, and four decisions shape them:

* **`apply_decision` is the step ticket 28 left open: the engine's answer, written
  onto the week.** It reconciles *every* sheet of the week — the original and each
  supplement, which have requests of their own — and records each transition in the
  trail, so "when did this week become locked" is answerable from this module rather
  than inferred from the engine's own record. It runs on the read and before every
  write, which is what makes a decision taken elsewhere visible on the very next
  request. An approval is what *locks* the week: from then on every write path in this
  module refuses, and `time_entries_guard_week_lock` refuses a console that never
  asked this module at all.

* **A correction is a reversal plus a new entry, written by `open_supplement`, and it
  lands in a sheet of its own.** The original is never touched — not one row of it —
  because it is what two approvers signed; what the week now says is the sum of the
  two sheets, and the net is arithmetic over rows rather than an overwritten figure.
  A reversal is the exact negation of one locked entry, which is why the entry table
  learned a sign: `original + reversal + new` is the expected value because the pair
  cancels, and a test can assert exactly that.

* **A reversal is not editable and neither is what it reverses.** The pair means
  something only while both halves still negate each other, so `update_entry` and
  `remove_entry` refuse a reversal with a code of its own, and the migration's trigger
  refuses the same two moves at the database. The *replacement* is an ordinary draft
  row: an employee who changes their mind about the new figure edits that, and the
  correction still cancels what it was always going to cancel.

* **The eight-week window is a lower bound, and beyond it nothing writes at all.**
  `supplement_weeks_left` counts the weeks a week still has; zero means the week is
  closed to every write path in this module — not only to the supplement endpoint,
  which is the half a rule like this usually gets wrong. The week is closed *and
  recorded*: the guard writes the row in `timesheet_weeks_lock` that
  `time_entries_guard_week_lock` reads, so the fact outlives the request that
  discovered it. Future weeks are deliberately unaffected: planning next week is not
  back-filling, and the window exists to close payroll history.

The service is the only thing that fetches rows, and it commits once per operation —
except when it refuses a closed week, where it commits the closing it just recorded
before it raises, which is the same rule `_editable` follows for a repaired cache.
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction, record
from app.domain.access.principal import Principal
from app.domain.approval.models import ApprovalState, ApprovalStatus, SubmitContext
from app.domain.attendance.business_day import madrid_today
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
    SUPPLEMENT_WINDOW_WEEKS,
    UNSET,
    CorrectionInput,
    DayTotal,
    EntryInput,
    EntryPatch,
    EntryType,
    OverBudgetDay,
    ProjectLabel,
    TaskNet,
    Timesheet,
    TimesheetEntry,
    TimesheetPage,
    TimesheetStatus,
    WeekView,
    assert_monday,
    monday_of,
    supplement_weeks_left,
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

#: Why the system closes a week. Stored on the lock row, so the record says which of
#: the two ways a week was closed rather than leaving a reader to guess from the date.
CLOSED_BY_WINDOW = "outside the eight-week supplementary window"


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

        It does apply what the engine has already decided, and that is a write: see
        `apply_decision`, which is the same read. Nothing else is written — the week's
        entries are read, never created, and the window is reported rather than
        enforced here, because a read is not a write whatever week it is about.
        """
        return await self.apply_decision(week_start)

    async def list_weeks(self, *, limit: int = 50, offset: int = 0) -> TimesheetPage:
        """The caller's own weeks, newest first.

        This is the 只能为本人填报 rule in its read form: the query is scoped to the
        caller's employee id and there is no parameter that could name somebody else.
        """
        return await self._repository.list_weeks(self.employee_id, limit=limit, offset=offset)

    async def apply_decision(self, week_start: date) -> WeekView:
        """Write what the engine has decided about the week back onto its sheets.

        **The step ticket 28 left open.** Its note said this module owns
        `apply_decision`: the week's status is a cache of the engine's answer, and
        something has to be the one that writes it. This is that something — the read
        is this method, `submit` reaches it through `_editable`, and a caller that has
        just driven a decision can call it directly to make the week catch up without
        waiting for somebody to open a page.

        Every sheet of the week, because a supplement has a request of its own: a week
        whose original is approved and whose correction was returned is two answers,
        and the grid needs both. Each move is recorded — `timesheet.decision_applied`
        — with the status it came from and the one it went to, which is what makes
        "when did this week lock" answerable from this module's own history rather
        than reconstructed from the engine's.

        **It commits, and an approval is why.** A lock that only existed inside one
        request would leave `time_entries_guard_week_lock` reading `pending` from the
        database, so a console could still write a week that two people had signed —
        the guarantee would be a property of the reader rather than of the row. Ticket
        28 could leave the repair to the caller because nothing outside this module
        depended on it; ticket 29's lock does.

        Idempotent, and it never moves a sheet backwards past an approval: the engine
        cannot un-approve, and a status this module wrote is only ever replaced by
        what the engine now says.
        """
        assert_monday(week_start)
        sheets = await self._decided_sheets(week_start)
        if sheets:
            await self._repository.commit()
        return await self._view(week_start, sheets)

    async def sheets_of(self, week_start: date) -> list[Timesheet]:
        """The week's sheets, oldest first, with the engine's decisions applied.

        For the status endpoint: the original and its corrections each carry their own
        round of approvals, and a client that wants to show "your correction is
        waiting for HR" needs the supplement's state beside the week's.
        """
        assert_monday(week_start)
        sheets = await self._decided_sheets(week_start)
        if sheets:
            await self._repository.commit()
        return sheets

    async def status_of(self, week_start: date) -> ApprovalState | None:
        """The week's approval request, with every round's steps and decisions.

        Read from the engine rather than mirrored here: its decisions are
        append-only and authoritative, and a copy would be a second answer to "who
        rejected this, when, and what did they say". `ApprovalState` spans all
        rounds, which is exactly the 历史提交记录 the ticket asks to keep — a
        resubmission opens a new round and the earlier decisions stay readable.

        `None` for a week that has never been filed, which is an answer and not an
        error: the client shows 草稿 beside it. This is the *original* sheet's request;
        a correction's own request is the engine's answer for the supplement's id,
        which `sheets_of` hands back.
        """
        assert_monday(week_start)
        sheet = await self._repository.get_week(self.employee_id, week_start)
        if sheet is None:
            return None
        return await self._approvals.state_of(ENTITY_TYPE, sheet.id)

    async def state_of_sheet(self, timesheet_id: UUID) -> ApprovalState | None:
        """One sheet's approval request, whoever's sheet it is *within this employee's week*.

        Asked with an id rather than a week because a week has several sheets once it
        has been corrected, and each has a round of its own. It is still the caller's
        own week: the route resolves the sheets from `sheets_of`, which is scoped to
        the caller's employee id, so an id from elsewhere is not reachable through it.
        """
        return await self._approvals.state_of(ENTITY_TYPE, timesheet_id)

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

        "The editable week" is a sheet rather than a week once a correction exists:
        the entry lands in the week's open sheet, which is the supplement while one is
        being filled in, and never in the locked original.
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
                "entry_type": EntryType.NORMAL,
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

        A **reversal** is refused here rather than edited: it is one half of a pair
        whose meaning is that the other half is its exact negation, and a reversal
        somebody moved is an original that no longer cancels. The entry a reversal
        points at is refused too, by the same rule seen from the other side.
        """
        assert_monday(week_start)
        sheet = await self._editable(week_start)
        entry = await self._require_entry(sheet, entry_id)
        self._require_not_reversal(entry)
        await self._require_not_reversed(entry)

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

        A reversal is the exception in the other direction: it may not be removed,
        because the row it cancels is locked and would otherwise come back to life
        with nothing to say so. The replacement beside it is an ordinary row and can
        be removed freely — "this entry should not exist at all" is exactly a reversal
        with no replacement.
        """
        assert_monday(week_start)
        sheet = await self._editable(week_start)
        entry = await self._require_entry(sheet, entry_id)
        self._require_not_reversal(entry)

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

        source = await self._standing_entries(source_start)
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

        **A correction files itself.** Once a supplement is open it is the week's
        editable sheet, so this files the correction — its reversals and its new rows
        — under a request of its own, and the original's approval is left exactly as it
        was. That is what "补充提交同样走两级审批" means in practice: one engine, one
        route, two documents.
        """
        assert_monday(week_start)
        sheet = await self._editable(week_start)

        filed = await self._repository.entries_in_sheet(sheet.id)
        if not filed:
            raise DomainError(
                TimesheetErrorCode.TIMESHEET_NOT_EDITABLE,
                detail=f"week {week_start} has no entries; there is nothing to file",
            )
        await self._require_still_recordable(filed)

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
        # The warning is a fact about the *week*, and the total is what this document
        # states: after a correction those are two different sums, and an approver
        # being told about the wrong one is the defect this distinction exists for.
        week_entries = await self._repository.entries_in_week(self.employee_id, week_start)
        await self._audit(
            AuditAction.TIMESHEET_SUBMITTED,
            sheet.id,
            after={
                "employee_id": self.employee_id,
                "week_start": week_start,
                "approval_request_id": request_id,
                "is_supplementary": sheet.is_supplementary,
                "total_minutes": sum(entry.minutes for entry in filed),
                "week_net_minutes": sum(entry.minutes for entry in week_entries),
                # The warning travels into the trail as well: a week filed with a
                # 9-hour day is the fact an approver is being asked about, and the
                # record of what the employee was told is worth keeping.
                "over_budget_days": [
                    day.entry_date.isoformat()
                    for day in _over_budget(week_entries, await self._expected(week_start))
                ],
            },
        )
        await self._repository.commit()
        # Read back rather than assembled: the totals and the over-budget warning are
        # computed in one place, so a submission and the read that follows it cannot
        # report different numbers.
        return await self.read_week(week_start)

    # --- supplementary submissions ------------------------------------------

    async def open_supplement(
        self, week_start: date, corrections: Sequence[CorrectionInput]
    ) -> WeekView:
        """Correct a locked week by opening a sheet of its own beside it.

        **The original is not edited — not one row of it.** That is the whole point of
        the ticket's 原记录一字不动, and it is why the correction is a document: what
        the week now comes to is the arithmetic of two sheets rather than a figure
        that overwrote what two approvers signed.

        Each correction writes a **pair**: a reversal that is the exact negation of the
        locked entry, and — unless the correction says the entry should not exist —
        the new positive entry that replaces it. The pair is written in the same
        transaction as the sheet, which is what makes `original + reversal + new` the
        net rather than a promise about it.

        Four refusals, in the order the caller would hit them: the week has to be
        inside the eight-week window (the global lock, refused before anything else),
        it has to be *locked* (there is nothing to correct about a draft — the employee
        edits it), no correction may already be in flight for it (two undecided
        supplements would be two answers to what the week says), and each correction
        has to name an entry of this week's original sheet that has not been reversed
        already. Everything a supplement writes then satisfies the same guards an
        ordinary entry does: `_recordable` resolves the target, so the project has to
        be active and inside its own dates.
        """
        assert_monday(week_start)
        await self._require_employee()
        await self._require_open_week(week_start)

        # Open the window's own books first: a correction is the one operation that is
        # *about* the passage of time, so it is where the weeks that have fallen out of
        # it get closed. Bounded to this employee's own sheets — a week nobody wrote is
        # unwritable by construction and does not need a row saying so.
        closed = await self._repository.lock_expired_weeks(
            self.employee_id, before=self._window_opens(), reason=CLOSED_BY_WINDOW
        )

        sheets = await self._decided_sheets(week_start)
        original = next((sheet for sheet in sheets if not sheet.is_supplementary), None)
        if original is None:
            raise DomainError(
                TimesheetErrorCode.TIMESHEET_NOT_FOUND,
                detail=f"the week of {week_start} has never been written",
            )
        if original.status is not TimesheetStatus.APPROVED:
            raise DomainError(
                TimesheetErrorCode.TIMESHEET_SUPPLEMENT_NOT_LOCKED,
                detail=(
                    f"week {week_start} is {original.status}; a supplement corrects a "
                    "locked week, and an open one is edited directly"
                ),
            )
        if not corrections:
            raise DomainError(
                TimesheetErrorCode.TIMESHEET_SUPPLEMENT_INVALID,
                detail="a supplement states at least one correction",
            )
        for sheet in sheets:
            if sheet.is_supplementary and (
                sheet.is_editable or sheet.status is TimesheetStatus.PENDING
            ):
                raise DomainError(
                    TimesheetErrorCode.TIMESHEET_SUPPLEMENT_OPEN,
                    detail=(
                        f"week {week_start} already has a supplement ({sheet.id}, "
                        f"{sheet.status}) that has not been decided"
                    ),
                )

        locked = {
            entry.id: entry
            for entry in await self._repository.entries_in_sheet(original.id)
        }
        # Everything is checked and resolved *before* the first row is written, so a
        # correction the project module would refuse leaves no reversal of a locked
        # entry behind. The alternative — write, then discover — depends on the caller
        # rolling the transaction back, which is a guarantee about somebody else.
        #: entry id -> (new minutes, target project, target task, resolved billable)
        resolved: dict[UUID, tuple[int | None, UUID, UUID, bool | None]] = {}
        seen: set[UUID] = set()
        for correction in corrections:
            source = locked.get(correction.entry_id)
            if source is None or correction.entry_id in seen:
                raise DomainError(
                    TimesheetErrorCode.TIMESHEET_SUPPLEMENT_INVALID,
                    detail=(
                        f"{correction.entry_id} is not an entry of the original sheet of "
                        f"week {week_start}, or it is named twice"
                    ),
                )
            seen.add(correction.entry_id)
            if correction.minutes is None:
                # A reversal with no replacement: "this should not have been recorded".
                resolved[source.id] = (None, source.project_id, source.task_id, None)
                continue
            self._require_minutes(correction.minutes)
            project_id = correction.project_id or source.project_id
            task_id = correction.task_id or source.task_id
            rules = await self._recordable(project_id, task_id, source.entry_date)
            resolved[source.id] = (correction.minutes, project_id, task_id, rules.is_billable)

        await self._require_room_for_correction(week_start, resolved, locked)

        supplement = await self._repository.create_supplement(
            self.employee_id, week_start, original.id
        )
        written: list[dict] = []
        for correction in corrections:
            # Keyed by the entry the correction named, so the pair below is written
            # from what was resolved for *that* entry rather than by position.
            minutes, project_id, task_id, billable = resolved[correction.entry_id]
            source = locked[correction.entry_id]
            reversal = await self._repository.add_entry(
                supplement.id,
                self.employee_id,
                week_start,
                EntryInput(
                    entry_date=source.entry_date,
                    project_id=source.project_id,
                    task_id=source.task_id,
                    # The exact negation, on the same day, task and billable flag: the
                    # pair cancels, and the trigger refuses anything that would not.
                    minutes=-source.minutes,
                    is_billable=source.is_billable,
                    entry_type=EntryType.REVERSAL,
                    reverses_entry_id=source.id,
                ),
            )
            await self._audit(
                AuditAction.TIMESHEET_ENTRY_WRITTEN,
                supplement.id,
                after={
                    "entry_id": reversal.id,
                    "entry_type": EntryType.REVERSAL,
                    "reverses_entry_id": source.id,
                    "entry_date": reversal.entry_date,
                    "project_id": reversal.project_id,
                    "task_id": reversal.task_id,
                    "minutes": reversal.minutes,
                    "is_billable": reversal.is_billable,
                },
                reason="a locked entry was reversed by a supplementary submission",
            )

            replacement: TimesheetEntry | None = None
            if minutes is not None:
                replacement = await self._repository.add_entry(
                    supplement.id,
                    self.employee_id,
                    week_start,
                    EntryInput(
                        entry_date=source.entry_date,
                        project_id=project_id,
                        task_id=task_id,
                        minutes=minutes,
                        # The resolved answer for the *new* target, which may differ from
                        # the original's: a correction that moves work to another task
                        # carries that task's billable configuration.
                        is_billable=bool(billable),
                        note=correction.note,
                    ),
                )
                await self._audit(
                    AuditAction.TIMESHEET_ENTRY_WRITTEN,
                    supplement.id,
                    after={
                        "entry_id": replacement.id,
                        "entry_type": EntryType.NORMAL,
                        "entry_date": replacement.entry_date,
                        "project_id": replacement.project_id,
                        "task_id": replacement.task_id,
                        "minutes": replacement.minutes,
                        "is_billable": replacement.is_billable,
                    },
                    reason="a supplementary submission restated a locked entry",
                )

            written.append(
                {
                    "reverses_entry_id": source.id,
                    "before_minutes": source.minutes,
                    "after_minutes": minutes,
                    "replacement_entry_id": None if replacement is None else replacement.id,
                }
            )

        await self._audit(
            AuditAction.TIMESHEET_SUPPLEMENT_OPENED,
            supplement.id,
            after={
                "employee_id": self.employee_id,
                "week_start": week_start,
                "supersedes_timesheet_id": original.id,
                "corrections": written,
                "weeks_closed": [week.isoformat() for week in closed],
            },
            reason=(
                f"a supplementary submission against the locked week of {week_start}, "
                f"with {len(written)} correction(s)"
            ),
        )
        await self._repository.commit()
        return await self.read_week(week_start)

    # --- internals ----------------------------------------------------------

    async def _view(self, week_start: date, sheets: list[Timesheet]) -> WeekView:
        """Assemble the grid: entries by day, the schedule's answer, the totals.

        One pass over the rows and one call for the seven days' expectations, rather
        than a query per day: a grid is seven cells, and the naive shape of this
        method is seven round trips for one screen.

        Every number here is computed over the week's **rows**, which is what makes a
        reversal visible rather than merely effective: the day's total is the net, and
        `gross_minutes` and `reversal_minutes` are what it was reached from, so a grid
        that shows six hours can also show that it was eight minus two.
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
                    gross_minutes=sum(row.minutes for row in rows if row.minutes > 0),
                    reversal_minutes=-sum(row.minutes for row in rows if row.minutes < 0),
                )
            )

        week = tuple(days)
        expected_total = (
            None
            if all(day.expected_minutes is None for day in week)
            else sum(day.expected_minutes or 0 for day in week)
        )
        original = next((item for item in sheets if not item.is_supplementary), None)
        supplements = tuple(item for item in sheets if item.is_supplementary)
        left = supplement_weeks_left(week_start, self._current_week())
        return WeekView(
            employee_id=self.employee_id,
            week_start=week_start,
            days=week,
            timesheet=original,
            entries_total_minutes=sum(day.total_minutes for day in week),
            expected_total_minutes=expected_total,
            over_budget_days=_over_budget(entries, expectations),
            sheets=tuple(sheets),
            supplements=supplements,
            editable_sheet_id=_editable_sheet_id(sheets),
            gross_total_minutes=sum(day.gross_minutes for day in week),
            reversal_total_minutes=sum(day.reversal_minutes for day in week),
            tasks=_task_nets(entries),
            supplement_weeks_left=left,
            week_closed=left == 0 or await self._repository.week_is_locked(week_start),
        )

    async def _expected(self, week_start: date) -> dict[date, DayExpectation]:
        return await self._expectations.day_expectations(
            self.employee_id, week_start, week_start + timedelta(days=DAYS_PER_WEEK - 1)
        )

    def _current_week(self) -> date:
        """The Monday this request is happening in, as the company's calendar counts it.

        Madrid's day rather than the container's, and the attendance module's own
        conversion rather than a second one here: the window is a statement about
        which week somebody is in, and two answers to that would move the boundary by
        a day twice a year. Injectable through `now`, which is what lets a test put a
        week on either side of the window without waiting eight weeks.
        """
        return monday_of(madrid_today(self._now()))

    def _window_opens(self) -> date:
        """The oldest week the window still covers.

        Strictly greater than this Monday: the week eight back has no weeks left, which
        is the number the refusal names.
        """
        return self._current_week() - timedelta(weeks=SUPPLEMENT_WINDOW_WEEKS)

    async def _standing_entries(self, week_start: date) -> list[TimesheetEntry]:
        """The week's entries as they now stand: what a copy of the week should carry.

        Rows that are still in force — a normal entry nothing has reversed — because a
        copy of a corrected week is a copy of the week as it now reads, not of the
        original plus the reversal plus the replacement. The reversals themselves are
        deliberately absent: they are a statement about *that* week's history, and
        copying one would cancel an entry in a week where nothing was ever recorded.
        """
        entries = await self._repository.entries_in_week(self.employee_id, week_start)
        reversed_ids = {entry.reverses_entry_id for entry in entries if entry.is_reversal}
        return [
            entry
            for entry in entries
            if entry.entry_type is EntryType.NORMAL and entry.id not in reversed_ids
        ]

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

    async def _decided_sheets(self, week_start: date) -> list[Timesheet]:
        """Every sheet of the week, with the engine's answer written back onto it.

        The column exists so a list of weeks does not cost a round trip per row, and a
        cache only ever written by this module's own `submit` would go stale the moment
        a manager decided — which is the one moment it matters. This is `apply_decision`
        for the whole week: the original and each supplement have requests of their own,
        so a single sheet's reconciliation would leave the correction's status saying
        whatever it said when it was filed.

        Nothing is committed here: a read that found a stale cache repairs it in the
        transaction it already owns, and the caller's commit ends it. A *refusal*
        commits what it repaired before it refuses — see `_editable`.
        """
        sheets = await self._repository.sheets_in_week(self.employee_id, week_start)
        decided: list[Timesheet] = []
        for sheet in sheets:
            decided.append(await self._apply_to(sheet))
        return decided

    async def _apply_to(self, sheet: Timesheet) -> Timesheet:
        """One sheet's status, moved to what the engine says and recorded when it moves."""
        state = await self._approvals.state_of(ENTITY_TYPE, sheet.id)
        if state is None:
            return sheet
        status = _STATUS_OF.get(state.status)
        if status is None or status is sheet.status:
            return sheet
        moved = await self._repository.set_status(sheet.id, status)
        await self._audit(
            AuditAction.TIMESHEET_DECISION_APPLIED,
            sheet.id,
            before={"status": sheet.status},
            after={
                "status": status,
                "engine_status": state.status,
                "round": state.round,
                "is_supplementary": sheet.is_supplementary,
                "week_start": sheet.week_start,
                "employee_id": self.employee_id,
            },
            reason=(
                "approved, and therefore locked for ever"
                if status is TimesheetStatus.APPROVED
                else f"the engine's answer is {state.status}"
            ),
            # The engine decided; this module only wrote down what it said, and
            # attributing the transition to whoever happened to open the page would be
            # a lie the trail tells about itself.
            initiated_by="system",
        )
        return moved

    async def _editable(self, week_start: date) -> Timesheet:
        """The week's *open* sheet, when this employee may still write in it.

        Three refusals in one place, and every write path in this module comes through
        here, which is what makes the global week lock a rule rather than a check the
        supplement endpoint happens to carry:

        * the week has to be inside the window (`_require_open_week`) — beyond it
          nothing writes at all;
        * the week has to have an open sheet, or be one nobody has written (the first
          write of a week creates the original);
        * that sheet has to be in a status the employee owns.

        A week whose sheets are all filed or locked is refused with the code that names
        *which*: a pending week is "not editable until a decision returns it", and an
        approved one is locked for ever with a supplement as the way through. The
        statuses are read through `_decided_sheets`, so the engine's latest decision is
        what decides — a week the manager just approved is locked on the next keystroke,
        not after a cache expiry.

        **A refusal commits the correction it just made.** Otherwise the one write path
        that discovers a stale cache is also the one that throws the repair away with
        its own rollback, and the row stays wrong until somebody's read happens to
        succeed — which, for a locked week, is never.
        """
        await self._require_employee()
        await self._require_open_week(week_start)

        sheets = await self._decided_sheets(week_start)
        open_sheet = next((sheet for sheet in sheets if sheet.is_editable), None)
        if open_sheet is not None:
            return open_sheet

        if not sheets:
            return await self._create(week_start)

        if any(sheet.is_locked for sheet in sheets) and all(
            not sheet.is_editable for sheet in sheets
        ):
            raise DomainError(
                TimesheetErrorCode.TIMESHEET_WEEK_LOCKED,
                detail=(
                    f"week {week_start} is approved and locked for ever; the way to "
                    "change it is a supplementary submission"
                ),
            )
        raise DomainError(
            TimesheetErrorCode.TIMESHEET_NOT_EDITABLE,
            detail=(
                f"week {week_start} is {sheets[-1].status}; a filed week is read-only "
                "until a decision returns it"
            ),
        )

    async def _require_open_week(self, week_start: date) -> None:
        """The global week lock: beyond the eight-week window no write path may write.

        The ticket's 全局周锁, and the reason it is one method rather than a check at
        the supplement route: a rule that only the correction path enforces is a rule
        every other path is a hole in.

        **The refusal writes down what it found.** The week is locked in this module's
        own table before the error is raised, so the closing has a timestamp and a
        reason rather than being re-derived from the clock on every later request — and
        so `time_entries_guard_week_lock` refuses a console that never asked this
        module anything. The commit before the raise is the same rule `_editable`
        follows for a repaired cache: what was learned must not be rolled back with
        the refusal that learned it.
        """
        left = supplement_weeks_left(week_start, self._current_week())
        if left > 0:
            return
        closed = await self._repository.lock_week(week_start, reason=CLOSED_BY_WINDOW)
        await self._audit(
            AuditAction.TIMESHEET_WEEK_LOCKED,
            None,
            after={
                "week_start": week_start,
                "employee_id": self.employee_id,
                "weeks_left": left,
                "reason": CLOSED_BY_WINDOW,
                "already_locked": not closed,
            },
            reason="a write was attempted in a week outside the supplementary window",
            initiated_by="system",
        )
        await self._repository.commit()
        raise DomainError(
            TimesheetErrorCode.TIMESHEET_WEEK_CLOSED,
            detail=(
                f"the week of {week_start} is outside the {SUPPLEMENT_WINDOW_WEEKS}-week "
                f"supplementary window: {left} of {SUPPLEMENT_WINDOW_WEEKS} weeks remain, "
                "and no write may touch it"
            ),
        )

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
            # table, and the route already names the week. An entry of *another sheet of
            # the same week* lands here too, which is the right answer: the locked
            # original's rows are not editable, whichever week they belong to.
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

    async def _require_room_for_correction(
        self,
        week_start: date,
        resolved: dict[UUID, tuple[int | None, UUID, UUID, bool | None]],
        locked: dict[UUID, TimesheetEntry],
    ) -> None:
        """The same per-day cap a write obeys, asked of what the correction will add.

        A correction writes up to two rows per changed entry — the reversal and the
        replacement — onto days that already hold the locked original's rows, so the
        grid's own limit is the one rule a supplement could otherwise walk past. The
        count is what will be true when the pairs are written, not what is true now.
        """
        entries = await self._repository.entries_in_week(self.employee_id, week_start)
        on_day: dict[date, int] = {}
        for entry in entries:
            on_day[entry.entry_date] = on_day.get(entry.entry_date, 0) + 1
        for entry_id, (minutes, _project, _task, _billable) in resolved.items():
            day = locked[entry_id].entry_date
            on_day[day] = on_day.get(day, 0) + 1 + (1 if minutes is not None else 0)

        for day, total in sorted(on_day.items()):
            if total > MAX_ENTRIES_PER_DAY:
                raise DomainError(
                    TimesheetErrorCode.INVALID_REQUEST,
                    detail=(
                        f"{day} would hold {total} entries after this correction; a day "
                        f"holds at most {MAX_ENTRIES_PER_DAY}"
                    ),
                )

    @staticmethod
    def _require_not_reversal(entry: TimesheetEntry) -> None:
        """A reversal is one half of a pair, and it is not the half anybody edits.

        Changing it would leave an original that no longer cancels; removing it would
        resurrect a locked entry with nothing to say so. The correction is unmade the
        other way round — by removing or editing the replacement, and by filing another
        supplement if the week itself is wrong.
        """
        if entry.is_reversal:
            raise DomainError(
                TimesheetErrorCode.TIMESHEET_ENTRY_IS_REVERSAL,
                detail=(
                    f"entry {entry.id} reverses {entry.reverses_entry_id}; a reversal is "
                    "the record of a locked entry being cancelled and may not be changed"
                ),
            )

    async def _require_not_reversed(self, entry: TimesheetEntry) -> None:
        """An entry that has been reversed is frozen: it is what the reversal negates."""
        reversal = await self._repository.reversal_for(entry.id)
        if reversal is not None:
            raise DomainError(
                TimesheetErrorCode.TIMESHEET_ENTRY_IS_REVERSAL,
                detail=(
                    f"entry {entry.id} was reversed by {reversal.id}; what a supplement "
                    "cancelled is not editable afterwards"
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
        sheet_id: UUID | None,
        *,
        before: dict | None = None,
        after: dict | None = None,
        reason: str | None = None,
        initiated_by: str = "user",
    ) -> None:
        """One record per state change, in the transaction that made it.

        `sheet_id` is `None` for the one event that is about a *week* rather than a
        document — the week being closed for good — which is why the column is
        nullable rather than the record being skipped: "somebody tried to write in a
        closed week" is exactly the attempt an incident review looks for.
        """
        await record(
            self._session,
            action=action,
            entity_type=ENTITY_TYPE,
            entity_id=sheet_id,
            before=before,
            after=after,
            reason=reason,
            initiated_by=initiated_by,
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


def _task_nets(entries: list[TimesheetEntry]) -> list[TaskNet]:
    """The week per task: what was recorded, what was reversed, and what stands.

    Ordered by project then task, which is an order a reader can follow rather than
    whichever order the rows came back in: the point of the list is comparing tasks,
    and a list that reshuffles between two reads cannot be compared with itself.
    """
    totals: dict[tuple[UUID, UUID], list[int]] = {}
    for entry in entries:
        key = (entry.project_id, entry.task_id)
        bucket = totals.setdefault(key, [0, 0])
        if entry.minutes > 0:
            bucket[0] += entry.minutes
        else:
            bucket[1] -= entry.minutes

    return [
        TaskNet(
            project_id=project_id,
            task_id=task_id,
            gross_minutes=gross,
            reversal_minutes=reversed_minutes,
            net_minutes=gross - reversed_minutes,
        )
        for (project_id, task_id), (gross, reversed_minutes) in sorted(
            totals.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))
        )
    ]


def _editable_sheet_id(sheets: list[Timesheet]) -> UUID | None:
    """Which sheet of the week the next write would land in, if there is one.

    The newest open sheet, because a week has at most one in practice: the original
    while it is a draft, or the supplement filed against a locked one. Handed to the
    client so a grid can draw the locked rows as read-only and the correction's rows
    as editable in the same table.
    """
    open_sheets = [sheet for sheet in sheets if sheet.is_editable]
    return open_sheets[-1].id if open_sheets else None


__all__ = [
    "CLOSED_BY_WINDOW",
    "ENTITY_TYPE",
    "MAX_ENTRIES_PER_DAY",
    "TimesheetService",
    "UNSET",
]
