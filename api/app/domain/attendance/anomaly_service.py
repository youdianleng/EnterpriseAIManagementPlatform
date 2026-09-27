"""The nightly scan, the reminder's two questions, and what resolves an anomaly.

`docs/DESIGN.md` §7.1 fixes what this is:

    夜间定时任务（worker）：扫描当日 attendance_daily …
    次日 08:00 → 汇总邮件给经理 + 站内提醒员工补卡

Three operations, and the module docstring is the place the ticket's decisions go.

**One row per person per day per kind, and the uniqueness is the idempotency.**
The scan derives what the day is missing and inserts it with `DO NOTHING` against
`uq_attendance_anomalies_day_type`, so a pass that runs twice — a retry, a
restarted container, two workers that overlapped — records the same facts once. A
check in this service would be the race the index exists to close, and the report
says which half happened (`created` versus `existing`) so "nothing was wrong" and
"nothing was written" are different answers.

**The scan never resolves anything.** Resolving means "somebody dealt with this, and
this is the event that did it" (`resolved_by_event_id`), which only the correction
flow can say: it is the one path that knows which event it appended and why. Ticket
24 calls `resolve_for_correction` after its approval, and the rule is a property of
the row rather than of this pass — an anomaly with a resolving event is resolved,
and nothing here re-opens one. A corollary worth stating: a shift that somebody
closes late, after the night's pass has already run, leaves its `missing_clock_out`
standing until a correction or a re-scan of that date says otherwise.

**A day nobody was expected to work is not examined at all.** Holidays and rest days
are zero minutes, employees on approved leave are asked of the leave seam (ticket 25
fills it), and neither is counted in `examined` — the pass's question is "who was
due today", and the answer is the population it looked at.

**One employee per query set, and one commit at the end.** The pass is a nightly job
over the whole company, so it is a loop of per-person reads rather than one clever
join across `employee_assignments`, `work_schedule_days` and `holidays`: those rules
belong to the scheduling module, and re-implementing them here in SQL is how the
scan and the day's own snapshot start disagreeing. A failure on one person is
reported and the pass continues.
"""

from collections.abc import Sequence
from datetime import date, datetime
from uuid import UUID

from app.domain.attendance.anomalies import (
    Anomaly,
    AnomalyFailure,
    AnomalyScanReport,
    NewAnomaly,
    detect,
    failure_of,
)
from app.domain.attendance.anomaly_repository import (
    AnomalyRepository,
    AssumeNoLeave,
    LeaveLookup,
)
from app.domain.attendance.models import ExpectationSource, TimeSource, utc_now
from app.domain.schedule.models import DayExpectation


class AnomalyService:
    """Scanning one day, and the two questions the morning reminder asks.

    `expectations` is the scheduling module seen through `ExpectationSource` — the
    same two-question seam the attendance service already takes — and `leave` is the
    hole ticket 25 fills. `now` is injectable for the reason every other service's
    is: `detected_at` is evidence of when somebody could have known, and a test that
    cannot pin it cannot assert it.
    """

    def __init__(
        self,
        repository: AnomalyRepository,
        *,
        expectations: ExpectationSource,
        leave: LeaveLookup | None = None,
        now: TimeSource = utc_now,
    ) -> None:
        self._repository = repository
        self._expectations = expectations
        self._leave = leave if leave is not None else AssumeNoLeave()
        self._now = now

    # --- the nightly pass ---------------------------------------------------

    async def scan(self, business_date: date) -> AnomalyScanReport:
        """Examine one day for everybody and record what is wrong with it.

        The date is the whole input, which is what makes the pass testable: a test
        drives any date and any punches through this method without waiting for a
        night to pass, and an operator can re-examine a day somebody has since
        corrected by running the job with that date.
        """
        examined = 0
        created: list[Anomaly] = []
        existing = 0
        failed: list[AnomalyFailure] = []

        for employee_id in await self._repository.employee_ids():
            try:
                expected = await self._expectations.day_expectation(
                    employee_id, business_date
                )
                if not expected.is_working_day:
                    continue
                examined += 1
                found = await self._detect(employee_id, business_date, expected)
            except Exception as error:  # noqa: BLE001 - reported, never fatal
                failed.append(failure_of(employee_id, error))
                continue

            for anomaly in found:
                written = await self._repository.insert(anomaly, detected_at=self._now())
                if written is None:
                    existing += 1
                else:
                    created.append(written)

        await self._repository.commit()
        return AnomalyScanReport(
            business_date=business_date,
            examined=examined,
            created=tuple(created),
            existing=existing,
            failed=tuple(failed),
        )

    # --- the reminder's questions ------------------------------------------

    async def unnotified(self, business_date: date) -> list[Anomaly]:
        """The day's anomalies that are still standing and still untold."""
        return await self._repository.unnotified(business_date)

    async def mark_notified(
        self, anomaly_ids: Sequence[UUID], *, at: datetime | None = None
    ) -> int:
        """Stamp the reminder on the rows it was raised for, and commit.

        Called after the notifications are raised rather than before: a stamp
        written first would make a crash between the two leave a day that nobody is
        ever reminded about.
        """
        stamp = at or self._now()
        marked = await self._repository.mark_notified(anomaly_ids, at=stamp)
        await self._repository.commit()
        return marked

    # --- reads --------------------------------------------------------------

    async def day_anomalies(self, employee_id: UUID, business_date: date) -> list[Anomaly]:
        """One person's one day, resolved ones included.

        The resolved rows are part of the answer on purpose: "this was flagged and
        here is what cleared it" is what somebody asking about a day wants, and a
        reader that only saw the open ones could not tell a clean day from a
        corrected one.
        """
        return await self._repository.day_anomalies(employee_id, business_date)

    # --- what a correction clears (ticket 24's entry point) -----------------

    async def resolve_for_correction(
        self, employee_id: UUID, business_date: date, event_id: UUID
    ) -> tuple[Anomaly, ...]:
        """Bring a corrected day up to date and close what the correction cleared.

        Ticket 24 calls this once its approval has appended the make-up event and
        the day has been recomputed. The question it answers is which of the day's
        anomalies are *still* true of it, and the answer is the same detection the
        nightly scan runs, asked again: an anomaly the day no longer shows is
        resolved by that event, and one it still shows is left open — a correction
        that moved a clock_in to 11:00 does not clear the lateness it created, and
        saying otherwise would put a false "somebody dealt with this" on the record.

        The anomalies the correction *created* are recorded here too, in the same
        transaction, so a re-examined day is never half updated. The one case the
        unique key cannot express is a kind that was already resolved once and is
        true again after a second correction: the day's own snapshot shows it and
        the resolved row keeps the name of the event that once cleared it.
        """
        standing = await self._detect(employee_id, business_date)
        await self._record(standing)

        kinds = {anomaly.type for anomaly in standing}
        open_rows = [
            row
            for row in await self._repository.day_anomalies(employee_id, business_date)
            if not row.is_resolved and row.type not in kinds
        ]
        resolved = (
            await self._repository.resolve([row.id for row in open_rows], event_id)
            if open_rows
            else []
        )
        await self._repository.commit()
        return tuple(resolved)

    # --- internals ----------------------------------------------------------

    async def _detect(
        self,
        employee_id: UUID,
        business_date: date,
        expected: DayExpectation | None = None,
    ) -> list[NewAnomaly]:
        """What this day is missing, from the stream and the schedule."""
        if expected is None:
            expected = await self._expectations.day_expectation(employee_id, business_date)
        return detect(
            employee_id=employee_id,
            business_date=business_date,
            events=await self._repository.events_for_day(employee_id, business_date),
            expected=expected,
            on_leave=await self._leave.is_on_leave(employee_id, business_date),
        )

    async def _record(self, found: Sequence[NewAnomaly]) -> None:
        """Insert whatever is not already recorded. Conflicts are the point."""
        for anomaly in found:
            await self._repository.insert(anomaly, detected_at=self._now())


__all__ = ["AnomalyService"]
