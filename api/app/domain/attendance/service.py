"""The attendance module: four operations, and the day is this module's business.

`docs/architecture/codebase-design.md` §2.4 fixes the interface:

    clock(employee_id, kind, at, source) -> Event
    day_view(employee_id, business_date) -> DayRecord
    recompute_day(employee_id, business_date) -> DayRecord
    range_view(employee_id, from_date, to_date) -> list[DayRecord]

**Everything takes and returns dates.** No method here accepts or returns a raw
timestamp for anything that aggregates by day, so a caller cannot confuse UTC with
the business calendar: the conversion happens once, inside `clock`, and every read
uses the stored `business_date`. That is the interface-level answer to the risk the
design register names, and it is why `clock`'s one timestamp argument is not
"the day" — it is the instant of a punch, which the module then places.

**`clock` recomputes the day in the same transaction.** A caller that clocks out
and immediately asks for the day sees the day that punch produced, not the one
before it: the append and the snapshot commit together or not at all.

**A replay is not a second punch.** The same employee, the same kind and the same
instant is the same row — the unique index says so, and this returns the row that
is already there instead of raising. A *different* instant while a shift is open is
refused, because that is a second press rather than a retry: the two cases look
alike from a log and are not alike at all from a timesheet.

Three rules the write path owns, all of them about *which* day a punch belongs to:

* a `clock_in` belongs to the Madrid calendar day of the instant;
* a `clock_out` belongs to the day of the shift it closes, so a shift that crosses
  midnight stays one day (`MAX_SHIFT` bounds how far back it will look);
* a clock_out with no shift to close — or one whose shift began longer ago than any
  shift lasts — is refused rather than recorded as an orphan. A punch nobody can
  pair is not evidence of work, and the earlier day keeps its `missing_out` so the
  anomaly is visible instead of silently closed.

One consequence of that last pair is worth stating, because it is deliberate: a
punch that arrives **out of order** — an offline punch synced after a later one has
already been recorded — is accepted and the day derives as `incomplete`, rather
than being re-paired into a shift. Re-ordering two punches to make a shift the
employee did not work would be inventing working time, and an anomaly somebody can
see is better than a number nobody can explain.
"""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from app.domain.attendance.business_day import business_date_of, dates_between, madrid_today
from app.domain.attendance.derivation import derive
from app.domain.attendance.errors import AttendanceErrorCode
from app.domain.attendance.models import (
    CLOCK_SKEW,
    MAX_RANGE_DAYS,
    MAX_SHIFT,
    PUNCH_EVENT_TYPES,
    TERMINATED_STATUS,
    AttendanceEvent,
    DayRecord,
    EventSource,
    EventType,
    ExpectationSource,
    NewEvent,
    TimeSource,
    utc_now,
)
from app.domain.attendance.repository import AttendanceRepository
from app.domain.errors import DomainError
from app.domain.schedule.models import DayExpectation


class AttendanceService:
    """The four operations. Nothing else is public.

    No session: a punch *is* its own record. There is no second row to write beside
    it — the stream is append-only and carries who, when and from where — so unlike
    the approval engine this module has nothing to audit into the same transaction.

    `now` is injectable because the design names the time source as a real seam:
    without it, "is this shift still running or was it never closed" and the two
    DST transitions cannot be decided deterministically, and a test that waits for
    midnight is not a test.
    """

    def __init__(
        self,
        repository: AttendanceRepository,
        *,
        now: TimeSource = utc_now,
        expectations: ExpectationSource | None = None,
    ) -> None:
        self._repository = repository
        self._now = now
        # Optional, and that is deliberate: without it a day has no expectation,
        # which is what ticket 21 shipped and what its tests still assert. The
        # request path always supplies it; a caller that only wants the punch
        # arithmetic does not have to.
        self._expectations = expectations

    # --- clocking ----------------------------------------------------------

    async def clock(
        self,
        employee_id: UUID,
        kind: EventType | str,
        at: datetime,
        source: EventSource | str,
        *,
        ip_address: str | None = None,
        created_by_employee_id: UUID | None = None,
    ) -> AttendanceEvent:
        """Append a punch and rebuild the day it belongs to.

        `source` is part of the interface because the same operation has to serve
        the web button (ticket 21) and, later, an imported or corrected punch; what
        it may *not* be is `correction`, which is a different act with a target and
        a reason attached (ticket 24). The two keyword-only arguments are the facts
        the caller knows and the module does not: where the request came from, and
        on whose behalf it was made.
        """
        event_type = _punch_type(kind)
        event_source = _web_source(source)
        instant = _timed(at)

        now = self._now()
        if instant > now + CLOCK_SKEW:
            raise DomainError(
                AttendanceErrorCode.EVENT_IN_FUTURE,
                detail=(
                    f"punch at {instant.isoformat()} is later than {now.isoformat()}; "
                    "working time is a record of what happened"
                ),
            )

        # Checked before anything else, so a retry that arrives after the employee
        # has been terminated, or after their shift closed, still gets the row it
        # is asking about rather than a refusal about the present.
        existing = await self._repository.find_punch(employee_id, event_type, instant)
        if existing is not None:
            return existing

        status = await self._require_employee(employee_id)
        if status == TERMINATED_STATUS and event_type is EventType.CLOCK_IN:
            # Clocking *in* is what closes the record; clocking out is allowed,
            # because a shift that was open when the termination was applied can
            # still be closed, and refusing it would freeze a `missing_out` the
            # person has no way to repair (ticket 18 owns the termination itself).
            raise DomainError(
                AttendanceErrorCode.EMPLOYEE_TERMINATED,
                detail=(
                    f"employee {employee_id} is terminated; a missing punch on a closed "
                    "record is a correction, not a new clock_in"
                ),
            )

        business_date = await self._day_of_punch(employee_id, event_type, instant)
        event = await self._repository.append_event(
            NewEvent(
                employee_id=employee_id,
                event_type=event_type,
                occurred_at=instant,
                business_date=business_date,
                source=event_source,
                ip_address=ip_address,
                created_by_employee_id=created_by_employee_id,
            )
        )
        # Same transaction: the caller that clocked out and asks for the day next
        # must not have to wait for a worker to agree with it.
        await self._rebuild(employee_id, business_date)
        await self._repository.commit()
        return event

    # --- reading -----------------------------------------------------------

    async def day_view(self, employee_id: UUID, business_date: date) -> DayRecord:
        """One day.

        The stored snapshot is the answer when there is one: it is what the module
        has already agreed the day was, and re-deriving it on every read would make
        the snapshot decorative. A day with no snapshot — nobody worked, or nobody
        has recomputed it yet — is derived here and not written down, because a
        read must not be a write.
        """
        await self._require_employee(employee_id)
        stored = await self._repository.day_record(employee_id, business_date)
        if stored is not None:
            return stored
        return await self._derive(employee_id, business_date)

    async def recompute_day(self, employee_id: UUID, business_date: date) -> DayRecord:
        """Rebuild one day's snapshot from its events, and replace what was there.

        Idempotent by construction: the day is derived from the events and written,
        never adjusted. Running it twice writes the same numbers, which is what
        makes it safe to call after a correction (ticket 24) or from a job.
        """
        await self._require_employee(employee_id)
        record = await self._rebuild(employee_id, business_date)
        await self._repository.commit()
        return record

    async def range_view(
        self, employee_id: UUID, from_date: date, to_date: date
    ) -> list[DayRecord]:
        """Every day in an inclusive range, including the ones nobody worked.

        Complete by construction, and that is the point: a month with three days
        off must not come back as a list of the days that happen to have rows. A
        day with no events is a day with no events — `absent` until ticket 22 can
        tell a holiday from an absence — and it is present in the answer as such.
        """
        await self._require_employee(employee_id)
        days = dates_between(from_date, to_date)
        if not days or len(days) > MAX_RANGE_DAYS:
            raise DomainError(
                AttendanceErrorCode.RANGE_INVALID,
                detail=(
                    f"{from_date} to {to_date} is not a range this module will answer: "
                    f"at most {MAX_RANGE_DAYS} days, earliest first"
                ),
            )

        stored = {
            record.business_date: record
            for record in await self._repository.day_records(employee_id, from_date, to_date)
        }
        gaps = [day for day in days if day not in stored]
        if not gaps:
            return [stored[day] for day in days]

        # One query for every day without a snapshot, however scattered: the gaps
        # in a month are usually the weekends, and a query per gap would turn a
        # calendar view into thirty round trips. The expectations come in one pass
        # for the same reason.
        events = await self._repository.events_by_date(employee_id, gaps[0], gaps[-1])
        expected = (
            await self._expectations.day_expectations(employee_id, gaps[0], gaps[-1])
            if self._expectations is not None
            else {}
        )
        today = madrid_today(self._now())
        return [
            stored[day]
            if day in stored
            else derive(
                employee_id=employee_id,
                business_date=day,
                events=events.get(day, []),
                today=today,
                expected=expected.get(day),
            )
            for day in days
        ]

    # --- internals ---------------------------------------------------------

    async def _require_employee(self, employee_id: UUID) -> str:
        """The employee's status, or a refusal.

        Every operation checks, including the reads: a day belonging to nobody is
        not an absent day, it is a question about somebody who does not exist, and
        answering it with a phantom `absent` record would put a row of nothing into
        a working-time record.
        """
        status = await self._repository.employee_status(employee_id)
        if status is None:
            raise DomainError(
                AttendanceErrorCode.EMPLOYEE_NOT_FOUND, detail=f"unknown employee {employee_id}"
            )
        return status

    async def _derive(self, employee_id: UUID, business_date: date) -> DayRecord:
        events = await self._repository.events_for_day(employee_id, business_date)
        return derive(
            employee_id=employee_id,
            business_date=business_date,
            events=events,
            today=madrid_today(self._now()),
            expected=await self._expectation(employee_id, business_date),
        )

    async def _expectation(
        self, employee_id: UUID, business_date: date
    ) -> DayExpectation | None:
        """What the schedule expected, or nothing when there is no scheduling module.

        One query, and only for the day being derived: the range read fetches its
        expectations in one pass of its own, because a month of days would otherwise
        be a month of round trips.
        """
        if self._expectations is None:
            return None
        return await self._expectations.day_expectation(employee_id, business_date)

    async def _rebuild(self, employee_id: UUID, business_date: date) -> DayRecord:
        """Derive the day and write it, stamped with the moment it was rebuilt.

        The stamp is applied here rather than by the database's `now()` so that the
        record this returns is the record a reader will find: a caller that
        recomputes a day and then reads it must not see two different rows.
        """
        derived = await self._derive(employee_id, business_date)
        record = replace(derived, recomputed_at=self._now())
        await self._repository.save_day(record)
        return record

    async def _day_of_punch(
        self, employee_id: UUID, event_type: EventType, instant: datetime
    ) -> date:
        """Which business day this punch counts against.

        The whole cross-midnight rule is the difference between the two branches:
        a clock_in takes its own day, and a clock_out takes the day of the shift it
        closes. Nothing else in the module ever looks at two days at once.
        """
        open_shift = await self._repository.latest_punch(employee_id)
        closes = (
            open_shift.event_type is EventType.CLOCK_IN if open_shift is not None else False
        )
        if event_type is EventType.CLOCK_IN:
            if closes and timedelta(0) <= instant - open_shift.occurred_at <= MAX_SHIFT:
                raise DomainError(
                    AttendanceErrorCode.ALREADY_CLOCKED_IN,
                    detail=(
                        f"a shift opened at {open_shift.occurred_at.isoformat()} is still "
                        f"open; {instant.isoformat()} is a second clock_in, not a new shift"
                    ),
                )
            return business_date_of(instant)

        if closes and timedelta(0) <= instant - open_shift.occurred_at <= MAX_SHIFT:
            # The shift started on some day; the punch belongs to that day, even
            # when the clock says the next one.
            return open_shift.business_date
        raise DomainError(
            AttendanceErrorCode.NO_OPEN_SHIFT,
            detail=(
                f"no shift opened at or before {instant.isoformat()} is still open"
                + (
                    f" (the last one began at {open_shift.occurred_at.isoformat()}, "
                    f"longer ago than the {MAX_SHIFT} a shift may last)"
                    if open_shift is not None
                    else ""
                )
            ),
        )


def _punch_type(kind: EventType | str) -> EventType:
    try:
        event_type = EventType(kind)
    except ValueError as exc:
        raise DomainError(
            AttendanceErrorCode.INVALID_REQUEST, detail=f"unknown event type {kind!r}"
        ) from exc
    if event_type not in PUNCH_EVENT_TYPES:
        raise DomainError(
            AttendanceErrorCode.CORRECTION_NOT_A_PUNCH,
            detail=(
                "a correction restates an event that exists and carries a target and a "
                "reason; it is appended by the correction flow, not by clock()"
            ),
        )
    return event_type


def _web_source(source: EventSource | str) -> EventSource:
    try:
        event_source = EventSource(source)
    except ValueError as exc:
        raise DomainError(
            AttendanceErrorCode.INVALID_REQUEST, detail=f"unknown source {source!r}"
        ) from exc
    if event_source is not EventSource.WEB:
        raise DomainError(
            AttendanceErrorCode.CORRECTION_NOT_A_PUNCH,
            detail=f"clock() records punches; source={event_source} is not one",
        )
    return event_source


def _timed(at: datetime) -> datetime:
    """Refuse a naive instant, in the vocabulary of the catalogue, and keep UTC.

    `business_day.business_date_of` raises `ValueError` for the same input. The
    conversion here is so a client that sent `2026-09-21T22:00:00` receives a
    catalogued 400 saying what was wrong, rather than an internal error whose
    message happens to mention timezones.

    The instant is normalised to UTC because every comparison downstream — how long
    a shift has been open, which of two punches came first — has to be on the
    absolute timeline. Two aware datetimes sharing a `tzinfo` are subtracted by
    Python *without* consulting the offset, so a client that sends Madrid on both
    sides of a DST transition would otherwise be measured against the wall clock.
    """
    if at.tzinfo is None or at.utcoffset() is None:
        raise DomainError(
            AttendanceErrorCode.INVALID_REQUEST,
            detail=f"the instant {at.isoformat()} carries no timezone",
        )
    return at.astimezone(UTC)


__all__ = ["AttendanceService"]
