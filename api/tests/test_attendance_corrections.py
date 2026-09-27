"""Ticket 24: the correction document, the chain it appends, and the record.

No mocks and no in-memory repository, for the reason `test_attendance_events.py`
gives: what this module has to get right is largely a *query* — which punch a
day-and-kind pair identifies, whether the original row is byte-for-byte what it was,
whether the runtime role can still rewrite a punch after a correction has been
applied, whether a date four years back is answered or silently cut off — and a
substitute would answer those with the test's own assumptions.

**The state the whole ticket is about is asserted as rows.** "The correction was
applied" is not a status field: it is a second event pointing at the first, a
snapshot rebuilt from both, an anomaly whose `resolved_by_event_id` names that
event, three audit records and a notification the engine raised. Every one of those
is a row, and every one of them is checked here.

**Almost everything is driven over HTTP**, because the wiring is half of what a new
surface can get wrong: a service that works while the endpoint builds a bare
`ApprovalService` would lose the notifications silently, and a router that forgets
the kernel would be a 200 for anybody. The service-level helpers exist for the two
things an endpoint cannot express — the applier's catch-up run, and the state corpus
the SQL `CASE` and the Python rule are compared over.

**The dates are fixed and in the past.** The flow refuses a correction of a day that
has not happened, so every date here is a Monday in September 2026 or earlier, and
nothing depends on the day the suite runs on except the four-year test, which
computes its date from the Madrid calendar on purpose: "four years ago" is the
requirement, and a hardcoded date would stop being four years ago.
"""

import csv
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from io import StringIO
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.core.errors import ErrorCode
from app.domain.access.kernel import Reason, Resource, ResourceKind, can
from app.domain.access.permissions import Action
from app.domain.access.principal import Principal
from app.domain.approval.models import DecisionKind
from app.domain.approval.service import ApprovalService
from app.domain.attendance.anomalies import AnomalyType
from app.domain.attendance.anomaly_service import AnomalyService
from app.domain.attendance.business_day import MADRID, madrid_today
from app.domain.attendance.correction_service import CorrectionService
from app.domain.attendance.corrections import CorrectionState, state_of_correction
from app.domain.attendance.errors import AttendanceErrorCode
from app.domain.attendance.models import (
    MAX_RANGE_DAYS,
    DayStatus,
    EventSource,
    EventType,
)
from app.domain.attendance.notify import AttendanceNotifier
from app.domain.attendance.records import EXPORT_COLUMNS, TOTAL_LABEL, AttendanceRecords
from app.domain.attendance.service import AttendanceService
from app.domain.errors import DomainError
from app.domain.notification.approval import ApprovalNotifier
from app.domain.notification.models import NotificationType
from app.domain.notification.service import NotificationService
from app.domain.schedule.models import ScheduleDayInput, ScheduleInput
from app.domain.schedule.service import ScheduleService
from app.repositories.approval import PostgresApprovalRepository
from app.repositories.attendance import (
    PostgresAnomalyRepository,
    PostgresAttendanceRepository,
    PostgresCorrectionRepository,
)
from app.repositories.notification import PostgresNotificationRepository
from app.repositories.schedule import PostgresScheduleRepository
from tests.support.platform import Actor, Platform

#: A Monday, and the name of the correction flow's own entity type in the audit
#: trail. Fixed rather than "today" so the punches, the window the schedule states
#: and the anomaly the scan raises all agree about which day is being discussed.
MONDAY = date(2026, 9, 21)

#: The department's window, and what a full day of it is worth. 09:00–17:00 with no
#: break is 480 minutes, which is what `work_schedule_days` requires the two to
#: agree on.
OPENS = time(9, 0)
CLOSES = time(17, 0)
FULL_DAY = 480

ENTITY_TYPE = "attendance_correction"

#: The clock values are pinned so a failure says what the flow read rather than what
#: the machine happened to think the time was.
CLOCKED_OUT = 16
SHOULD_HAVE_BEEN = 18


def at(day: date, hour: int, minute: int = 0) -> datetime:
    """An instant as somebody in Madrid would say it, through `ZoneInfo`."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=MADRID)


@dataclass(slots=True, frozen=True)
class Cast:
    """The people a correction moves between, and the one who has no part in it."""

    department: str
    position: str
    #: Files their own corrections, and is the subject of everybody else's.
    subject: Actor
    #: The subject's manager: level one, and a report of nobody's business but ours.
    manager: Actor
    #: Files corrections about other people (事后修正) — level one is their manager.
    hr: Actor
    #: Decides level two. A different holder of `hr`, since nobody decides their own.
    other_hr: Actor
    #: A colleague in the same department who does *not* report to the manager.
    outsider: Actor


@pytest.fixture
async def cast(platform: Platform) -> Cast:
    """The organisation: one department, one subject, their manager, HR, a colleague.

    The outsider is in the *same department* deliberately: "my department" is the
    reading of a manager's reach that this ticket refuses, and a test whose
    non-report sits in another department would pass for the wrong reason.
    """
    department = await platform.department("operaciones")
    position = await platform.position(department, "technician")
    # The department works Monday to Friday, so the scan has something to say about
    # the day the corrections in this file are about.
    async with platform.factory() as session:
        await ScheduleService(PostgresScheduleRepository(session), session).create_schedule(
            ScheduleInput(
                code="OPS-WEEK",
                name_es="Semana",
                name_en="Week",
                days=tuple(
                    ScheduleDayInput(
                        weekday=weekday,
                        expected_minutes=FULL_DAY,
                        start_time=OPENS,
                        end_time=CLOSES,
                    )
                    for weekday in range(5)
                ),
                department_id=UUID(department),
            )
        )

    manager = await platform.account(roles=("manager",))
    await platform.assign(manager.employee_id, department, position)
    subject = await platform.account(roles=("employee",))
    await platform.assign(
        subject.employee_id, department, position, manager_employee_id=manager.employee_id
    )
    hr = await platform.account(roles=("hr",))
    await platform.assign(
        hr.employee_id, department, position, manager_employee_id=manager.employee_id
    )
    other_hr = await platform.grant_account(roles=("hr",))
    outsider = await platform.account(roles=("employee",))
    await platform.assign(outsider.employee_id, department, position)

    return Cast(
        department=department,
        position=position,
        subject=subject,
        manager=manager,
        hr=hr,
        other_hr=other_hr,
        outsider=outsider,
    )


# --- the module on its own session, the way a request or a job uses it --------


@asynccontextmanager
async def attendance(platform: Platform) -> AsyncIterator[AttendanceNotifier]:
    """The service as the endpoints build it: wrapped, so a punch notifies."""
    async with platform.factory() as session:
        punches = PostgresAttendanceRepository(session)
        yield AttendanceNotifier(
            AttendanceService(
                punches,
                expectations=ScheduleService(PostgresScheduleRepository(session), session),
            ),
            NotificationService(PostgresNotificationRepository(session), session),
            punches,
        )


@asynccontextmanager
async def corrections(platform: Platform) -> AsyncIterator[CorrectionService]:
    """The correction flow, wired as the router wires it."""
    async with platform.factory() as session:
        punches = PostgresAttendanceRepository(session)
        expectations = ScheduleService(PostgresScheduleRepository(session), session)
        approvals = PostgresApprovalRepository(session)
        yield CorrectionService(
            PostgresCorrectionRepository(session),
            session,
            punches=punches,
            attendance=AttendanceService(punches, expectations=expectations),
            anomalies=AnomalyService(
                PostgresAnomalyRepository(session), expectations=expectations
            ),
            approvals=ApprovalNotifier(
                engine=ApprovalService(approvals, session),
                notifications=NotificationService(
                    PostgresNotificationRepository(session), session
                ),
                approvals=approvals,
            ),
        )


@asynccontextmanager
async def records(platform: Platform) -> AsyncIterator[AttendanceRecords]:
    async with platform.factory() as session:
        punches = PostgresAttendanceRepository(session)
        yield AttendanceRecords(
            punches,
            AttendanceService(
                punches,
                expectations=ScheduleService(PostgresScheduleRepository(session), session),
            ),
            PostgresAnomalyRepository(session),
        )


@asynccontextmanager
async def anomalies(platform: Platform) -> AsyncIterator[AnomalyService]:
    async with platform.factory() as session:
        yield AnomalyService(
            PostgresAnomalyRepository(session),
            expectations=ScheduleService(PostgresScheduleRepository(session), session),
        )


@asynccontextmanager
async def notifications(platform: Platform) -> AsyncIterator[NotificationService]:
    async with platform.factory() as session:
        yield NotificationService(PostgresNotificationRepository(session), session)


# --- helpers -----------------------------------------------------------------


async def worked(
    platform: Platform, employee_id: str, day: date = MONDAY, *, start: int = 9, end: int = 17
) -> None:
    """A complete shift on one day, through the real write path."""
    async with attendance(platform) as service:
        await service.clock(employee_id, EventType.CLOCK_IN, at(day, start), EventSource.WEB)
        await service.clock(employee_id, EventType.CLOCK_OUT, at(day, end), EventSource.WEB)


async def request_correction(
    actor: Actor,
    *,
    day: date = MONDAY,
    kind: str = "clock_out",
    at_hour: int = SHOULD_HAVE_BEEN,
    at_minute: int = 0,
    reason: str = "I clocked out at 18:00, not 16:00",
    employee_id: str | None = None,
    corrected_at: str | None = None,
    business_date: str | None = None,
):
    """The four facts the ticket names, as a request body."""
    body: dict = {
        "business_date": business_date or day.isoformat(),
        "kind": kind,
        "corrected_at": corrected_at or at(day, at_hour, at_minute).isoformat(),
        "reason": reason,
    }
    if employee_id is not None:
        body["employee_id"] = employee_id
    return await actor.post("/api/v1/attendance/corrections", json=body)


async def filed(actor: Actor, **kwargs) -> dict:
    """A draft that exists, filed and ready to be decided."""
    drafted = await request_correction(actor, **kwargs)
    assert drafted.status_code == 201, drafted.text
    correction_id = drafted.json()["id"]
    filed_response = await actor.post(f"/api/v1/attendance/corrections/{correction_id}/submit")
    assert filed_response.status_code == 200, filed_response.text
    return filed_response.json()


async def approve(
    actor: Actor, correction_id: str, *, decision: str = "approve", comment: str | None = "ok"
) -> dict:
    response = await actor.post(
        f"/api/v1/attendance/corrections/{correction_id}/decide",
        json={"decision": decision, "comment": comment},
    )
    return response


async def fully_approved(platform: Platform, cast: Cast, correction_id: str) -> dict:
    """Both levels, through the endpoints, and the answer the last one gave."""
    first = await approve(cast.manager, correction_id)
    assert first.status_code == 200, first.text
    second = await approve(cast.hr, correction_id)
    assert second.status_code == 200, second.text
    return second.json()


async def correction_row(platform: Platform, correction_id: str) -> tuple:
    rows = await platform.sql(
        "SELECT employee_id, business_date, kind, corrected_at, reason, "
        "requested_by_employee_id, approval_request_id, applied_event_id, applied_at "
        "FROM attendance_corrections WHERE id = :id",
        {"id": correction_id},
    )
    assert len(rows) == 1, f"expected one correction row, found {len(rows)}"
    return rows[0]


async def event_rows(platform: Platform, employee_id: str) -> list[tuple]:
    """Every row of the stream, with the columns a correction may not touch."""
    return await platform.sql(
        "SELECT id, event_type, occurred_at, business_date, source, ip_address, "
        "created_by_employee_id, correction_of_event_id, reason "
        "FROM attendance_events WHERE employee_id = :id ORDER BY created_at, id",
        {"id": employee_id},
    )


async def audit_actions(platform: Platform, correction_id: str) -> list[str]:
    """Everything recorded against one correction, oldest first.

    One filter, because every record about a document is written under the
    document's own type and id — including the engine's, which is what makes "who
    decided this, and when" answerable without a second copy of the decision.
    """
    rows = await platform.sql(
        "SELECT action FROM audit_log WHERE entity_type = :type AND entity_id = :id "
        "ORDER BY id",
        {"type": ENTITY_TYPE, "id": correction_id},
    )
    return [row[0] for row in rows]


async def anomaly_rows(platform: Platform, employee_id: str, day: date = MONDAY) -> list[tuple]:
    return await platform.sql(
        "SELECT type, resolved_by_event_id FROM attendance_anomalies "
        "WHERE employee_id = :id AND business_date = :day ORDER BY type",
        {"id": employee_id, "day": day},
    )


async def day_read(
    actor: Actor, *, day: date = MONDAY, employee_id: str | None = None, path: str = "punches"
):
    """One person's day, through the self-service surface."""
    params: dict = {"business_date": day.isoformat()}
    if employee_id is not None:
        params["employee_id"] = employee_id
    return await actor.get(f"/api/v1/attendance/{path}", params=params)


async def records_of(
    platform: Platform, cast: Cast, day: date = MONDAY, *, actor: Actor | None = None
) -> list[dict]:
    """The punch chains one day reads as, through the endpoint, as its owner."""
    response = await day_read(actor or cast.subject, day=day)
    assert response.status_code == 200, response.text
    return response.json()["punches"]


async def export(
    actor: Actor,
    *,
    from_date: date,
    to_date: date,
    employee_id: str | None = None,
):
    params: dict = {"from_date": from_date.isoformat(), "to_date": to_date.isoformat()}
    if employee_id is not None:
        params["employee_id"] = employee_id
    return await actor.get("/api/v1/attendance/export", params=params)


# --- the document -------------------------------------------------------------


async def test_a_correction_request_carries_the_four_facts(platform: Platform, cast: Cast) -> None:
    """The ticket's first line, as a stored row and as an audit record.

    A document with no day, no kind, no instant or no reason cannot be read by the
    approver who has to judge it, so all four are required and all four are kept.
    """
    await worked(platform, cast.subject.employee_id, start=9, end=CLOCKED_OUT)

    response = await request_correction(cast.subject)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == CorrectionState.DRAFT.value
    assert body["employee_id"] == cast.subject.employee_id
    assert body["requested_by_employee_id"] == cast.subject.employee_id
    assert body["business_date"] == MONDAY.isoformat()
    assert body["kind"] == "clock_out"
    assert datetime.fromisoformat(body["corrected_at"]) == at(MONDAY, SHOULD_HAVE_BEEN)
    assert body["reason"] == "I clocked out at 18:00, not 16:00"
    assert body["applied_event_id"] is None, "a draft has changed nothing yet"

    row = await correction_row(platform, body["id"])
    assert row[2] == "clock_out" and row[4].startswith("I clocked out")
    assert (row[5], row[6], row[7], row[8]) == (
        UUID(cast.subject.employee_id),
        None,
        None,
        None,
    ), "an unfiled draft names no request and has applied nothing"
    assert await audit_actions(platform, body["id"]) == [
        "attendance_correction.requested"
    ]
    # Nothing about the punch has moved: a request is a request.
    assert [event[1] for event in await event_rows(platform, cast.subject.employee_id)] == [
        "clock_in",
        "clock_out",
    ]


async def test_a_request_with_no_reason_or_no_timezone_is_refused(
    platform: Platform, cast: Cast
) -> None:
    """Two malformed requests, and the catalogue answers both.

    The reason is the schema's minimum length — a correction nobody can explain is
    what the approver is being asked to judge — and the naive instant is the
    module's own code, because `2026-09-21T18:00:00` is a wall clock that cannot be
    attributed to a business day.
    """
    await worked(platform, cast.subject.employee_id, start=9, end=CLOCKED_OUT)

    no_reason = await request_correction(cast.subject, reason="")
    naive = await request_correction(cast.subject, corrected_at="2026-09-21T18:00:00")

    assert no_reason.status_code == 422, no_reason.text
    assert naive.status_code == 422, naive.text
    assert naive.json()["error"]["code"] == ErrorCode.ATTENDANCE_CORRECTION_INVALID.value
    assert await platform.scalar("SELECT count(*) FROM attendance_corrections") == 0


async def test_a_correction_of_a_day_that_has_not_happened_is_refused(
    platform: Platform, cast: Cast
) -> None:
    """A correction restates a punch that happened, on a day that has.

    Both halves are checked: the instant is the check the clock already makes (a
    minute of skew is a client whose clock is fast), and the business date is the
    one a correction needs, because "the clock_out of tomorrow should have been
    18:00" is not a correction of anything.
    """
    today = madrid_today(datetime.now(UTC))
    tomorrow = today + timedelta(days=1)

    # A day that has not happened, with an instant that has: the business date is
    # the only thing wrong with it.
    future_day = await request_correction(
        cast.subject, day=tomorrow, corrected_at=at(today, 10).isoformat()
    )
    future_instant = await request_correction(
        cast.subject, day=MONDAY, corrected_at=at(tomorrow, 9).isoformat()
    )

    assert future_day.status_code == 422, future_day.text
    assert future_day.json()["error"]["code"] == ErrorCode.ATTENDANCE_CORRECTION_INVALID.value
    assert future_instant.status_code == 422, future_instant.text
    assert (
        future_instant.json()["error"]["code"]
        == ErrorCode.ATTENDANCE_EVENT_IN_FUTURE.value
    )
    assert await platform.scalar("SELECT count(*) FROM attendance_corrections") == 0


async def test_a_day_with_two_clock_outs_cannot_be_corrected(
    platform: Platform, cast: Cast
) -> None:
    """The one thing the document cannot express, refused rather than guessed.

    A split day has two shifts and therefore two clock_outs, and "the clock_out of
    that day" does not say which one is meant. Restating the wrong punch would leave
    a working-time record wrong in a way nobody asked about, so the request is
    refused while it is still a draft.
    """
    async with attendance(platform) as service:
        for start, end in ((8, 12), (13, 17)):
            await service.clock(
                cast.subject.employee_id, EventType.CLOCK_IN, at(MONDAY, start), EventSource.WEB
            )
            await service.clock(
                cast.subject.employee_id, EventType.CLOCK_OUT, at(MONDAY, end), EventSource.WEB
            )

    response = await request_correction(cast.subject, kind="clock_out", at_hour=19)

    assert response.status_code == 409, response.text
    assert (
        response.json()["error"]["code"]
        == ErrorCode.ATTENDANCE_CORRECTION_TARGET_UNRESOLVED.value
    )
    assert await platform.scalar("SELECT count(*) FROM attendance_corrections") == 0


async def test_a_terminated_record_can_still_be_corrected(
    platform: Platform, cast: Cast
) -> None:
    """The clock's own refusal says where a closed record is repaired.

    `clock` refuses a clock_in for somebody who has left, with a message naming the
    correction flow. So the correction flow has to accept exactly that case — a
    missing punch on a closed record — or the message would be pointing at a door
    that is also shut.
    """
    await platform.sql(
        "UPDATE employees SET status = 'terminated', termination_date = :day WHERE id = :id",
        {"day": MONDAY, "id": cast.subject.employee_id},
    )

    drafted = await request_correction(
        cast.subject, kind="clock_in", at_hour=9, reason="I worked that morning"
    )

    assert drafted.status_code == 201, drafted.text
    correction_id = drafted.json()["id"]
    assert (await cast.subject.post(
        f"/api/v1/attendance/corrections/{correction_id}/submit"
    )).status_code == 200
    await fully_approved(platform, cast, correction_id)

    rows = await event_rows(platform, cast.subject.employee_id)
    assert [(row[1], row[4]) for row in rows] == [("clock_in", "correction")]


async def test_a_request_about_somebody_else_is_hrs_and_refused_for_everybody_else(
    platform: Platform, cast: Cast
) -> None:
    """事后修正 is HR's act, and the subject of it is named on the document.

    A colleague — and the manager whose report it is — cannot file a correction
    about somebody else: managers *decide* corrections, they do not raise them, and
    the refusal is the catalogued 403 every other refusal in this API is.
    """
    await worked(platform, cast.subject.employee_id, start=9, end=CLOCKED_OUT)

    refused = await request_correction(cast.outsider, employee_id=cast.subject.employee_id)
    manager = await request_correction(cast.manager, employee_id=cast.subject.employee_id)
    allowed = await request_correction(cast.hr, employee_id=cast.subject.employee_id)

    for response, action in (
        (refused, Action.ATTENDANCE_CORRECTION_ANY),
        (manager, Action.ATTENDANCE_CORRECTION_ANY),
    ):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == ErrorCode.FORBIDDEN.value
        refusals = await platform.sql(
            "SELECT after FROM audit_log WHERE action = 'access.refused'"
        )
        assert any(row[0]["action"] == str(action) for row in refusals), refusals

    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["employee_id"] == cast.subject.employee_id
    assert allowed.json()["requested_by_employee_id"] == cast.hr.employee_id


# --- two levels, and the append ---------------------------------------------------


async def test_both_levels_are_needed_and_the_approval_appends(
    platform: Platform, cast: Cast
) -> None:
    """The whole ticket in one flow: request, manager, HR, and the record moves.

    The assertion that matters is the pair of them: after level one the punch is
    **byte-for-byte what it was** and the day still reads 16:00, and after level two
    there is a second event pointing at the first while the first is still exactly
    the row it was. A correction is an append, or it is not this design.
    """
    await worked(platform, cast.subject.employee_id, start=9, end=CLOCKED_OUT)
    original = await platform.sql(
        "SELECT id FROM attendance_events WHERE employee_id = :id AND event_type = 'clock_out'",
        {"id": cast.subject.employee_id},
    )
    original_event_id = original[0][0]
    before = await event_rows(platform, cast.subject.employee_id)

    draft = await request_correction(cast.subject)
    correction_id = draft.json()["id"]
    submitted = await cast.subject.post(f"/api/v1/attendance/corrections/{correction_id}/submit")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["state"] == CorrectionState.IN_APPROVAL.value

    at_manager = await approve(cast.manager, correction_id)
    assert at_manager.status_code == 200, at_manager.text
    assert at_manager.json()["state"] == CorrectionState.IN_APPROVAL.value, "HR has not seen it"
    assert await event_rows(platform, cast.subject.employee_id) == before, (
        "level one appended something"
    )
    assert await platform.scalar(
        "SELECT worked_minutes FROM attendance_daily WHERE employee_id = :id",
        {"id": cast.subject.employee_id},
    ) == 420, "the day moved before the second level agreed"

    at_hr = await approve(cast.hr, correction_id)
    assert at_hr.status_code == 200, at_hr.text
    applied = at_hr.json()

    assert applied["state"] == CorrectionState.APPLIED.value
    assert applied["applied_event_id"] is not None
    rows = await event_rows(platform, cast.subject.employee_id)
    assert len(rows) == 3, "the correction is a new row"
    appended = [row for row in rows if row[1] == "correction"]
    assert len(appended) == 1
    assert appended[0][7] == original_event_id, "the correction does not point at the punch"
    assert appended[0][4] == "correction"
    assert appended[0][8].startswith("I clocked out")
    # The original is untouched — the same nine columns, in the same order, as
    # before anything was approved.
    assert rows[0] == before[0] and rows[1] == before[1], "the original row was rewritten"
    assert await platform.scalar(
        "SELECT worked_minutes FROM attendance_daily WHERE employee_id = :id",
        {"id": cast.subject.employee_id},
    ) == 540
    assert await platform.scalar(
        "SELECT status FROM attendance_daily WHERE employee_id = :id",
        {"id": cast.subject.employee_id},
    ) == "ok"


async def test_only_the_resolved_approver_decides(platform: Platform, cast: Cast) -> None:
    """Two levels means two people, and nobody else gets a vote.

    Level one belongs to the manager the route resolved; level two to a holder of
    `hr` who is not the requester. The engine answers all of it, and this endpoint
    restates none of it — which is why the assertions are on the refusals.
    """
    await worked(platform, cast.subject.employee_id, start=9, end=CLOCKED_OUT)
    filed_correction = await filed(cast.subject)
    correction_id = filed_correction["id"]

    by_outsider = await approve(cast.outsider, correction_id)
    by_hr_too_early = await approve(cast.hr, correction_id)

    assert by_outsider.status_code == 403, by_outsider.text
    assert by_hr_too_early.status_code == 403, by_hr_too_early.text
    assert by_hr_too_early.json()["error"]["code"] == ErrorCode.APPROVAL_NOT_APPROVER.value
    assert await platform.scalar("SELECT count(*) FROM attendance_events") == 2

    assert (await approve(cast.manager, correction_id)).status_code == 200
    # The requester may not decide their own, at either level.
    assert (await approve(cast.subject, correction_id)).status_code == 403
    assert (await approve(cast.other_hr, correction_id)).status_code == 200


async def test_a_draft_is_editable_and_a_filed_document_is_not(
    platform: Platform, cast: Cast
) -> None:
    """The returned-for-correction half of the engine, made usable.

    A document that came back to its requester has to be *correctable*, or the
    engine's third decision would have no consequence. A filed one is not editable:
    two people were asked to approve what it says.
    """
    await worked(platform, cast.subject.employee_id, start=9, end=CLOCKED_OUT)
    drafted = await request_correction(cast.subject)
    correction_id = drafted.json()["id"]

    changed = await cast.subject.patch(
        f"/api/v1/attendance/corrections/{correction_id}",
        json={"corrected_at": at(MONDAY, 19).isoformat(), "reason": "19:00, sorry"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["reason"] == "19:00, sorry"
    assert datetime.fromisoformat(changed.json()["corrected_at"]) == at(MONDAY, 19)

    await cast.subject.post(f"/api/v1/attendance/corrections/{correction_id}/submit")
    refused = await cast.subject.patch(
        f"/api/v1/attendance/corrections/{correction_id}", json={"reason": "again"}
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == ErrorCode.ATTENDANCE_CORRECTION_NOT_DRAFT.value
    assert (await correction_row(platform, correction_id))[4] == "19:00, sorry"


async def test_a_returned_correction_can_be_corrected_and_filed_again(
    platform: Platform, cast: Cast
) -> None:
    """A return opens a new round; the first round's decision stays readable.

    This is the engine's history rather than a copy of it: the detail endpoint shows
    both rounds, and the second submission is the *same* request at round two.
    """
    await worked(platform, cast.subject.employee_id, start=9, end=CLOCKED_OUT)
    filed_correction = await filed(cast.subject)
    correction_id = filed_correction["id"]

    returned = await approve(cast.manager, correction_id, decision="return", comment="which time?")
    assert returned.status_code == 200, returned.text
    assert returned.json()["state"] == CorrectionState.DRAFT.value

    await cast.subject.patch(
        f"/api/v1/attendance/corrections/{correction_id}",
        json={"corrected_at": at(MONDAY, 18, 30).isoformat()},
    )
    again = await cast.subject.post(f"/api/v1/attendance/corrections/{correction_id}/submit")

    assert again.status_code == 200, again.text
    assert again.json()["approval"]["round"] == 2
    assert [item["decision"] for item in again.json()["approval"]["decisions"]] == ["returned"]

    applied = await fully_approved(platform, cast, correction_id)

    assert applied["state"] == CorrectionState.APPLIED.value
    detail = await cast.subject.get(f"/api/v1/attendance/corrections/{correction_id}")
    assert [item["decision"] for item in detail.json()["approval"]["decisions"]] == [
        "returned",
        "approved",
        "approved",
    ], "the first round's return has to stay readable"
    chains = await records_of(platform, cast, MONDAY)
    corrected = next(chain for chain in chains if chain["punch"]["event_type"] == "clock_out")
    assert datetime.fromisoformat(corrected["effective_at"]) == at(MONDAY, 18, 30)


async def test_a_rejection_is_final_for_the_document_and_a_new_one_replaces_it(
    platform: Platform, cast: Cast
) -> None:
    """A rejected request cannot be filed again; a new document can be raised.

    That is the engine's own rule — a rejection is final *for the request* — and the
    ticket's answer to a wrong correction: the chain grows a document rather than
    rewriting one.
    """
    await worked(platform, cast.subject.employee_id, start=9, end=CLOCKED_OUT)
    filed_correction = await filed(cast.subject)
    correction_id = filed_correction["id"]

    rejected = await approve(cast.manager, correction_id, decision="reject", comment="no")
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["state"] == CorrectionState.REJECTED.value
    assert await platform.scalar("SELECT count(*) FROM attendance_events") == 2, (
        "a rejection changed the record"
    )

    refused = await cast.subject.post(f"/api/v1/attendance/corrections/{correction_id}/submit")
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == ErrorCode.ATTENDANCE_CORRECTION_NOT_DRAFT.value
    assert "is rejected" in refused.json()["error"]["detail"], (
        "the refusal does not say that the request was rejected"
    )

    second = await filed(cast.subject, at_hour=19, reason="19:00, really")
    await fully_approved(platform, cast, second["id"])

    assert await platform.scalar("SELECT count(*) FROM attendance_corrections") == 2
    assert (await records_of(platform, cast, MONDAY))[0]["effective_at"] is not None


async def test_a_correction_the_engine_will_not_file_is_refused_in_this_modules_words(
    platform: Platform, cast: Cast
) -> None:
    """A route that resolves to nobody is the engine's refusal, told in this module's
    vocabulary.

    The client routes on the code, and `ERR_APR_003` would tell somebody looking at
    their own correction nothing; the engine's own code travels in the detail, which
    is where an operator needs it.
    """
    department = await platform.department("sinjefe")
    position = await platform.position(department, "solo")
    lonely = await platform.account(roles=("employee",))
    await platform.assign(lonely.employee_id, department, position)
    await worked(platform, lonely.employee_id, start=9, end=CLOCKED_OUT)

    drafted = await request_correction(lonely)
    assert drafted.status_code == 201, drafted.text
    refused = await lonely.post(
        f"/api/v1/attendance/corrections/{drafted.json()['id']}/submit"
    )

    assert refused.status_code == 409, refused.text
    assert (
        refused.json()["error"]["code"]
        == ErrorCode.ATTENDANCE_CORRECTION_SUBMISSION_REFUSED.value
    )
    assert "no approver configured" in refused.json()["error"]["detail"], (
        "the engine's own words do not travel with the refusal"
    )
    assert (await correction_row(platform, drafted.json()["id"]))[6] is None, (
        "a refused submission recorded a request"
    )


async def test_a_second_correction_chains_onto_the_correction(
    platform: Platform, cast: Cast
) -> None:
    """The chain, in order, and where a second correction points.

    `original → correction → correction` is what the screen shows and what the day
    resolves: a second request about the same punch continues the chain rather than
    branching off the original beside it, so the evolution of one punch reads as a
    sequence instead of as two competing values.
    """
    await worked(platform, cast.subject.employee_id, start=9, end=CLOCKED_OUT)
    first = await filed(cast.subject, at_hour=18, reason="18:00, not 16:00")
    await fully_approved(platform, cast, first["id"])
    first_event = (await correction_row(platform, first["id"]))[7]

    second = await filed(cast.subject, at_hour=19, reason="and it was 19:00 after all")
    await fully_approved(platform, cast, second["id"])
    second_event = (await correction_row(platform, second["id"]))[7]

    rows = await event_rows(platform, cast.subject.employee_id)
    chain = [row for row in rows if row[7] is not None]
    assert len(chain) == 2
    assert chain[0][7] != chain[1][7], "both corrections point at the same row"
    assert chain[0][0] == first_event
    assert chain[1][7] == first_event, "the second correction did not continue the chain"
    # And the record is what the chain says, not what either row alone says.
    assert await platform.scalar(
        "SELECT worked_minutes FROM attendance_daily WHERE employee_id = :id",
        {"id": cast.subject.employee_id},
    ) == 600
    read = await records_of(platform, cast, MONDAY)
    corrected = next(chain for chain in read if chain["punch"]["event_type"] == "clock_out")
    assert [row["id"] for row in corrected["corrections"]] == [
        str(first_event),
        str(second_event),
    ]
    assert corrected["is_corrected"] is True
    assert corrected["is_made_up"] is False
    assert datetime.fromisoformat(corrected["effective_at"]) == at(MONDAY, 19)


async def test_a_missing_clock_out_is_made_up_and_the_anomaly_is_resolved(
    platform: Platform, cast: Cast
) -> None:
    """The make-up case, which is the one ticket 23's test could not build.

    A forgotten clock_out has no row to point at, so an approved correction appends
    the punch itself: the day becomes `ok`, the `missing_clock_out` the night's pass
    raised is resolved *by that event*, and a later scan leaves it resolved.
    """
    async with attendance(platform) as service:
        await service.clock(
            cast.subject.employee_id, EventType.CLOCK_IN, at(MONDAY, 9), EventSource.WEB
        )
    async with anomalies(platform) as service:
        scanned = await service.scan(MONDAY)
    assert await anomaly_rows(platform, cast.subject.employee_id) == [
        (AnomalyType.MISSING_CLOCK_OUT.value, None)
    ], "the day was not in the state this test is about"

    filed_correction = await filed(cast.subject, at_hour=17, reason="I forgot to clock out")
    applied = await fully_approved(platform, cast, filed_correction["id"])

    assert applied["state"] == CorrectionState.APPLIED.value
    rows = await event_rows(platform, cast.subject.employee_id)
    made_up = [row for row in rows if row[1] == "clock_out"]
    assert len(made_up) == 1
    assert made_up[0][4] == "correction", "the made-up punch does not say how it got there"
    assert made_up[0][7] is None, "there was no punch for it to correct"
    assert await platform.scalar(
        "SELECT worked_minutes FROM attendance_daily WHERE employee_id = :id",
        {"id": cast.subject.employee_id},
    ) == FULL_DAY
    assert await anomaly_rows(platform, cast.subject.employee_id) == [
        (AnomalyType.MISSING_CLOCK_OUT.value, made_up[0][0])
    ]
    assert scanned.created_count >= 1

    async with anomalies(platform) as service:
        again = await service.scan(MONDAY)
    assert again.created_count == 0, "a later scan re-opened a resolved anomaly"
    read = await records_of(platform, cast, MONDAY)
    made_up_chain = next(
        chain for chain in read if chain["punch"]["event_type"] == "clock_out"
    )
    assert made_up_chain["is_made_up"] is True
    assert made_up_chain["is_corrected"] is False
    day = await day_read(cast.subject)
    assert day.json()["day"]["status"] == DayStatus.OK.value


async def test_a_correction_that_leaves_the_day_late_resolves_nothing(
    platform: Platform, cast: Cast
) -> None:
    """The half that would be tempting to get wrong, through the whole flow.

    Moving a clock_in from 09:30 to 09:45 does not clear the lateness — it is a
    lateness either way — and marking it resolved would put a false "somebody dealt
    with this" on the record the manager reads. Ticket 23 asserts the rule over
    hand-built events; this asserts that the flow calls it with the day it just
    re-derived.
    """
    async with attendance(platform) as service:
        await service.clock(
            cast.subject.employee_id, EventType.CLOCK_IN, at(MONDAY, 9, 30), EventSource.WEB
        )
        await service.clock(
            cast.subject.employee_id, EventType.CLOCK_OUT, at(MONDAY, 17), EventSource.WEB
        )
    async with anomalies(platform) as service:
        await service.scan(MONDAY)
    assert await anomaly_rows(platform, cast.subject.employee_id) == [
        (AnomalyType.LATE.value, None)
    ], "the day was not in the state this test is about"

    filed_correction = await filed(
        cast.subject, kind="clock_in", at_hour=9, at_minute=45, reason="I was still late"
    )
    applied = await fully_approved(platform, cast, filed_correction["id"])

    assert applied["state"] == CorrectionState.APPLIED.value
    assert await anomaly_rows(platform, cast.subject.employee_id) == [
        (AnomalyType.LATE.value, None)
    ], "a lateness the day still shows was marked resolved"
    chains = await records_of(platform, cast, MONDAY)
    corrected = next(chain for chain in chains if chain["punch"]["event_type"] == "clock_in")
    assert datetime.fromisoformat(corrected["effective_at"]) == at(MONDAY, 9, 45)


# --- the record the employee is entitled to see ---------------------------------


async def test_an_employee_reads_their_own_punches_and_derived_day(
    platform: Platform, cast: Cast
) -> None:
    """打卡明细与推导结果: the punches, the chain, the day and what was flagged."""
    await worked(platform, cast.subject.employee_id, start=9, end=17)

    response = await day_read(cast.subject)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["employee_id"] == cast.subject.employee_id
    assert body["business_date"] == MONDAY.isoformat()
    assert body["day"]["worked_minutes"] == FULL_DAY
    assert body["day"]["status"] == DayStatus.OK.value
    assert [chain["punch"]["event_type"] for chain in body["punches"]] == [
        "clock_in",
        "clock_out",
    ]
    assert all(chain["corrections"] == [] for chain in body["punches"])
    assert all(chain["is_made_up"] is False for chain in body["punches"])
    assert body["anomalies"] == []


async def test_a_date_four_years_ago_is_answerable_and_nothing_truncates_before_it(
    platform: Platform, cast: Cast
) -> None:
    """The four-year obligation, and the failure mode it rules out.

    Two dates, four and six years back, both punched through the real write path and
    both read back: the second is there to prove the first is not an accident of a
    query with a quiet lower bound somewhere. What *is* bounded is one request's
    width — the export refuses a range wider than the retention window — and that
    refusal is asserted here too, because it is the difference between a file about a
    period and a dump of the table.
    """
    today = madrid_today(datetime.now(UTC))
    four_years_ago = today - timedelta(days=4 * 365)
    six_years_ago = today - timedelta(days=6 * 365)
    # Oldest first: the stream is a clock, and a punch that arrives out of order is
    # refused rather than re-paired (the rule `test_attendance_events.py` states).
    await worked(platform, cast.subject.employee_id, six_years_ago, start=8, end=16)
    await worked(platform, cast.subject.employee_id, four_years_ago, start=8, end=16)

    old = await day_read(cast.subject, day=four_years_ago)
    older = await day_read(cast.subject, day=six_years_ago)

    assert old.status_code == 200, old.text
    assert old.json()["day"]["worked_minutes"] == FULL_DAY
    assert old.json()["day"]["status"] == DayStatus.OK.value
    assert old.json()["punches"][0]["punch"]["occurred_at"].startswith(
        four_years_ago.isoformat()
    ), "the four-year-old punch came back with the wrong instant"
    assert older.status_code == 200, older.text
    assert older.json()["day"]["worked_minutes"] == FULL_DAY, (
        "a date six years back was cut off, so the query has a lower bound it does not"
        " state"
    )

    window_start = today - timedelta(days=MAX_RANGE_DAYS - 1)
    dump = await export(cast.subject, from_date=window_start, to_date=today)
    too_wide = await export(cast.subject, from_date=six_years_ago, to_date=today)

    assert dump.status_code == 200, dump.text
    rows = list(csv.reader(StringIO(dump.text)))
    assert len(rows) == MAX_RANGE_DAYS + 2, "the window is not the days it claims"
    assert rows[1][2] == four_years_ago.isoformat(), (
        "the oldest day of the window is missing, so the export has a bound of its own"
    )
    assert six_years_ago.isoformat() not in dump.text, "a day outside the window travelled"
    assert too_wide.status_code == 422, too_wide.text
    assert too_wide.json()["error"]["code"] == ErrorCode.ATTENDANCE_RANGE_INVALID.value


async def test_the_export_lists_every_day_with_a_total_and_the_documented_columns(
    platform: Platform, cast: Cast
) -> None:
    """The file an accountant or a labour inspector reads, column by column.

    Every day of the range is a row — including the ones nobody worked, because a
    record that listed only the days with punches would answer "what did they do"
    rather than "what does the record say" — the order is the calendar's, the
    instants are Madrid's, the minutes are minutes, and the last row is the range
    total.
    """
    await worked(platform, cast.subject.employee_id, MONDAY, start=9, end=17)
    tuesday = MONDAY + timedelta(days=1)

    response = await export(cast.subject, from_date=MONDAY, to_date=tuesday)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="attendance-{cast.subject.employee_id}-{MONDAY}-{tuesday}.csv"'
    )
    rows = list(csv.reader(StringIO(response.text)))
    assert rows[0] == list(EXPORT_COLUMNS)
    assert len(rows) == 4, "one header, two days, one total"
    # `"Martín, Ana"` is one field, quoted by the writer rather than by a rule.
    assert rows[1][0].endswith(", Ada"), f"the name column is not a full name: {rows[1][0]}"
    worked_row = next(row for row in rows[1:3] if row[2] == MONDAY.isoformat())
    assert worked_row[3] == DayStatus.OK.value
    assert worked_row[4] == at(MONDAY, 9).isoformat()
    assert worked_row[5] == at(MONDAY, 17).isoformat()
    assert worked_row[6] == str(FULL_DAY)
    absent_row = next(row for row in rows[1:3] if row[2] == tuesday.isoformat())
    assert absent_row[3] == DayStatus.ABSENT.value
    assert absent_row[6] == "0"
    assert rows[1][2] < rows[2][2], "the rows are not in calendar order"
    assert rows[3][0] == TOTAL_LABEL
    assert rows[3][2] == f"{MONDAY}..{tuesday}"
    assert rows[3][6] == str(FULL_DAY)


# --- who may read somebody else's record ----------------------------------------


async def test_a_manager_reads_a_report_and_not_a_colleague(
    platform: Platform, cast: Cast
) -> None:
    """The escalation this ticket exists to refuse: "manager" is not "everybody".

    The colleague is in the manager's own department, which is exactly what the
    kernel's ordinary path would have read as permission; the refusal names the
    action that was attempted, and it is on the record.
    """
    await worked(platform, cast.subject.employee_id, start=9, end=17)

    report = await day_read(cast.manager, employee_id=cast.subject.employee_id)
    colleague = await day_read(cast.manager, employee_id=cast.outsider.employee_id)
    report_export = await export(
        cast.manager,
        from_date=MONDAY,
        to_date=MONDAY,
        employee_id=cast.subject.employee_id,
    )
    colleague_export = await export(
        cast.manager,
        from_date=MONDAY,
        to_date=MONDAY,
        employee_id=cast.outsider.employee_id,
    )

    assert report.status_code == 200, report.text
    assert report.json()["day"]["worked_minutes"] == FULL_DAY
    assert report_export.status_code == 200, report_export.text
    for response in (colleague, colleague_export):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == ErrorCode.FORBIDDEN.value
        assert "worked_minutes" not in response.text
    refusals = await platform.sql("SELECT after FROM audit_log WHERE action = 'access.refused'")
    assert any(
        row[0]["action"] == str(Action.ATTENDANCE_READ_REPORT) for row in refusals
    ), f"the refusal was not recorded against the action attempted: {refusals}"


async def test_hr_reads_anybody_and_an_employee_reads_only_their_own(
    platform: Platform, cast: Cast
) -> None:
    """HR's remit is the company's record; everybody else's is their own.

    Both directions, so neither answer can be explained away: the same request as HR
    is a 200 and as a colleague is a 403, and the colleague's own record is a 200.
    """
    await worked(platform, cast.outsider.employee_id, start=9, end=17)

    as_hr = await day_read(cast.hr, employee_id=cast.outsider.employee_id)
    as_colleague = await day_read(cast.subject, employee_id=cast.outsider.employee_id)
    own = await day_read(cast.subject)

    assert as_hr.status_code == 200, as_hr.text
    assert as_hr.json()["day"]["worked_minutes"] == FULL_DAY
    assert as_colleague.status_code == 403, as_colleague.text
    refusals = await platform.sql("SELECT after FROM audit_log WHERE action = 'access.refused'")
    assert any(
        row[0]["action"] == str(Action.ATTENDANCE_READ_REPORT) for row in refusals
    ), refusals
    assert own.status_code == 200, own.text


async def test_a_manager_reads_a_reports_corrections_and_a_colleagues_not(
    platform: Platform, cast: Cast
) -> None:
    """A correction request is part of the record, so it is read by the same rule."""
    drafted = await request_correction(cast.subject)
    assert drafted.status_code == 201, drafted.text

    report = await cast.manager.get(
        "/api/v1/attendance/corrections", params={"employee_id": cast.subject.employee_id}
    )
    colleague = await cast.manager.get(
        "/api/v1/attendance/corrections", params={"employee_id": cast.outsider.employee_id}
    )
    detail = await cast.manager.get(f"/api/v1/attendance/corrections/{drafted.json()['id']}")

    assert report.status_code == 200, report.text
    assert [item["id"] for item in report.json()["items"]] == [drafted.json()["id"]]
    assert detail.status_code == 200, detail.text
    assert detail.json()["approval"] is None, "an unfiled document has no request"
    assert colleague.status_code == 403, colleague.text


def test_the_kernel_refuses_a_manager_somebody_who_does_not_report_to_them() -> None:
    """The rule itself, over the dimension the HTTP tests cannot vary cheaply.

    A manager holds `attendance.read_report` and reaches *their reports* — the same
    `reports_employee_ids` the approval route is resolved from. Sharing a department
    is not part of it: the generic employee path would have allowed this case, which
    is why the action has a branch of its own. The control is the first assertion, so
    the refusal below cannot be a rule that refuses everybody.
    """
    report_employee_id, colleague_employee_id = uuid4(), uuid4()
    department = uuid4()
    manager = Principal(
        user_id=uuid4(),
        employee_id=uuid4(),
        username="jefa",
        roles=frozenset({"manager", "employee"}),
        clearance_level="medium",
        department_ids=frozenset({department}),
        primary_department_id=department,
        is_manager=True,
        reports_employee_ids=frozenset({report_employee_id}),
    )

    def attendance(owner: UUID) -> Resource:
        # No department: the resource is a *person's record*, and the department
        # they sit in is not part of who may read it.
        return Resource(ResourceKind.EMPLOYEE, owner_employee_id=owner)

    allowed = can(manager, Action.ATTENDANCE_READ_REPORT, attendance(report_employee_id))
    refused = can(manager, Action.ATTENDANCE_READ_REPORT, attendance(colleague_employee_id))

    assert allowed.allowed, allowed.detail
    assert allowed.primary_reason is Reason.MANAGER_OF_SUBJECT
    assert refused.denied, f"a manager reached a colleague: {refused.detail}"
    assert refused.primary_reason is Reason.NOT_MANAGER_OF_SUBJECT
    # And the self-only action still refuses everybody else's, whatever role they
    # hold — the line ticket 21 drew and this ticket did not move.
    assert can(manager, Action.ATTENDANCE_READ_OWN, attendance(report_employee_id)).denied
    for role in ("finance", "compliance", "it", "admin"):
        principal = Principal(
            user_id=uuid4(),
            employee_id=uuid4(),
            username="ana",
            roles=frozenset({role, "employee"}),
            reports_employee_ids=frozenset({report_employee_id}),
        )
        assert can(principal, Action.ATTENDANCE_READ_ALL, attendance(report_employee_id)).denied
        assert can(
            principal, Action.ATTENDANCE_READ_REPORT, attendance(report_employee_id)
        ).denied


# --- the audit trail and the notification ---------------------------------------


async def test_every_state_change_is_audited(platform: Platform, cast: Cast) -> None:
    """Requested, filed, decided and applied — one filter over one document.

    The middle of the three moments is the engine's own record, written under this
    document's entity type and id; a second copy under this module's name would be a
    copy that eventually disagrees with the engine.
    """
    await worked(platform, cast.subject.employee_id, start=9, end=CLOCKED_OUT)
    draft = await request_correction(cast.subject)
    correction_id = draft.json()["id"]
    await cast.subject.post(f"/api/v1/attendance/corrections/{correction_id}/submit")
    await fully_approved(platform, cast, correction_id)

    assert await audit_actions(platform, correction_id) == [
        "attendance_correction.requested",
        "approval.submitted",
        "approval.decided",
        "approval.decided",
        "attendance_correction.applied",
    ]
    applied = await platform.sql(
        "SELECT after, actor_user_id FROM audit_log WHERE entity_type = :type "
        "AND entity_id = :id AND action = 'attendance_correction.applied'",
        {"type": ENTITY_TYPE, "id": correction_id},
    )
    assert len(applied) == 1
    fields = applied[0][0]
    assert fields["kind"] == "clock_out"
    assert fields["event_type"] == "correction"
    assert fields["corrected_event_id"] is not None, "the trail does not name what it corrected"
    assert applied[0][1] == UUID(cast.hr.user_id), "the applied record does not name who acted"


async def test_the_outcome_notification_is_the_engines(platform: Platform, cast: Cast) -> None:
    """The requester hears about the outcome, and the engine is what says so.

    Ticket 24 raises no notification of its own: the approval notifier does, which
    is why the payload carries the engine's request id and the outcome token rather
    than anything this module invented.
    """
    await worked(platform, cast.subject.employee_id, start=9, end=CLOCKED_OUT)
    filed_correction = await filed(cast.subject)
    correction_id = filed_correction["id"]
    await fully_approved(platform, cast, correction_id)

    async with notifications(platform) as centre:
        requester = await centre.list_for(UUID(cast.subject.employee_id))
        other_hr = await centre.list_for(UUID(cast.other_hr.employee_id))

    outcomes = [item for item in requester.items if item.type is NotificationType.APPROVAL_APPROVED]
    assert len(outcomes) == 1, f"expected one outcome notification, got {requester.items}"
    assert outcomes[0].entity_type == ENTITY_TYPE
    assert UUID(str(outcomes[0].entity_id)) == UUID(correction_id)
    assert outcomes[0].payload["approval_request_id"] == filed_correction["approval"][
        "request_id"
    ]
    assert outcomes[0].payload["outcome"] == "approved"
    assert all(
        item.entity_id != UUID(correction_id) or item.type is NotificationType.APPROVAL_APPROVED
        for item in requester.items
    ), "this module raised a notification of its own"
    # The second level was somebody's turn before it was anybody's decision, and the
    # hand-off is the engine's notification rather than an outcome.
    assert [item.type for item in other_hr.items] == [
        NotificationType.APPROVAL_AWAITING_DECISION
    ]


# --- the applier, and the states it moves between -------------------------------


async def test_an_approved_but_unapplied_correction_is_applied_by_the_next_run(
    platform: Platform, cast: Cast
) -> None:
    """The gap between the engine's yes and the append, and how it closes.

    Approval is the engine's transaction and the append is this module's, so a crash
    between them leaves a document that is approved and has changed nothing — visible
    as `state=approved`, never as a punch that moved by itself. `apply_approved` is
    the catch-up, and it is idempotent: the second run applies nothing.
    """
    await worked(platform, cast.subject.employee_id, start=9, end=CLOCKED_OUT)
    filed_correction = await filed(cast.subject)
    correction_id = filed_correction["id"]

    # The engine decides both levels directly, the way a worker or a script would.
    async with platform.factory() as session:
        engine = ApprovalService(PostgresApprovalRepository(session), session)
        state = await engine.state_of(ENTITY_TYPE, UUID(correction_id))
        assert state is not None
        await engine.decide(state.id, UUID(cast.manager.employee_id), DecisionKind.APPROVE)
        await engine.decide(state.id, UUID(cast.other_hr.employee_id), DecisionKind.APPROVE)

    waiting = await cast.subject.get(f"/api/v1/attendance/corrections/{correction_id}")
    assert waiting.json()["state"] == CorrectionState.APPROVED.value
    assert waiting.json()["applied_event_id"] is None
    assert len(await event_rows(platform, cast.subject.employee_id)) == 2

    async with corrections(platform) as service:
        first = await service.apply_approved()
        second = await service.apply_approved()

    assert [item.id for item in first.applied] == [UUID(correction_id)]
    assert first.failed == ()
    assert second.applied == () and second.failed == ()
    applied = await cast.subject.get(f"/api/v1/attendance/corrections/{correction_id}")
    assert applied.json()["state"] == CorrectionState.APPLIED.value
    assert len(await event_rows(platform, cast.subject.employee_id)) == 3


async def test_the_query_state_and_the_pure_state_agree(platform: Platform, cast: Cast) -> None:
    """Two expressions of one rule: the list's `CASE` and `state_of_correction`.

    The list computes the state in SQL so a page is one round trip; the detail
    computes it in Python. A state added to one and not the other would make the same
    document read differently in the two views, and nothing else would notice.
    """
    await worked(platform, cast.subject.employee_id, start=9, end=CLOCKED_OUT)
    ids: dict[str, str] = {}
    ids["draft"] = (await request_correction(cast.subject, at_hour=18)).json()["id"]
    ids["in_approval"] = (await filed(cast.subject, at_hour=18, reason="one"))["id"]
    ids["rejected"] = (await filed(cast.subject, at_hour=18, reason="two"))["id"]
    await approve(cast.manager, ids["rejected"], decision="reject")
    ids["approved"] = (await filed(cast.subject, at_hour=18, reason="three"))["id"]
    ids["applied"] = (await filed(cast.subject, at_hour=18, reason="four"))["id"]
    await fully_approved(platform, cast, ids["applied"])

    async with platform.factory() as session:
        engine = ApprovalService(PostgresApprovalRepository(session), session)
        state = await engine.state_of(ENTITY_TYPE, UUID(ids["approved"]))
        assert state is not None
        await engine.decide(state.id, UUID(cast.manager.employee_id), DecisionKind.APPROVE)
        await engine.decide(state.id, UUID(cast.other_hr.employee_id), DecisionKind.APPROVE)

    withdrawn = await filed(cast.subject, at_hour=18, reason="five")
    ids["withdrawn"] = withdrawn["id"]
    async with platform.factory() as session:
        engine = ApprovalService(PostgresApprovalRepository(session), session)
        state = await engine.state_of(ENTITY_TYPE, UUID(ids["withdrawn"]))
        assert state is not None
        await engine.withdraw(state.id, UUID(cast.subject.employee_id))

    listed = await cast.subject.get("/api/v1/attendance/corrections", params={"limit": 100})
    assert listed.status_code == 200, listed.text
    from_query = {item["id"]: item["state"] for item in listed.json()["items"]}

    async with platform.factory() as session:
        repository = PostgresCorrectionRepository(session)
        engine = ApprovalService(PostgresApprovalRepository(session), session)
        from_python: dict[str, str] = {}
        for label, correction_id in ids.items():
            correction = await repository.get_correction(UUID(correction_id))
            assert correction is not None, label
            approval = await engine.state_of(ENTITY_TYPE, UUID(correction_id))
            from_python[correction_id] = state_of_correction(
                correction, approval.status if approval is not None else None
            ).value

    assert from_query == from_python
    assert set(from_python.values()) == {state.value for state in CorrectionState}, (
        f"the corpus does not exercise every state: {from_python}"
    )


# --- no in-place overwrite, anywhere --------------------------------------------


@pytest.fixture
async def app_connection(settings: Settings) -> AsyncIterator[async_sessionmaker]:
    """Sessions bound to the restricted role, on the test database."""
    restricted = create_async_engine(settings.runtime_test_database_url)
    try:
        yield async_sessionmaker(bind=restricted, expire_on_commit=False)
    finally:
        await restricted.dispose()


async def test_a_punch_can_only_change_through_an_approved_correction(
    platform: Platform, cast: Cast, app_connection: async_sessionmaker
) -> None:
    """The ticket's last line, from the side that can actually enforce it.

    There is no endpoint that overwrites a punch, and the reason that is a property
    of the system rather than of the router is PostgreSQL: the role requests connect
    as holds INSERT and SELECT on the stream and nothing else, so an in-place edit is
    refused by the database whatever code arrives later. The controls are the other
    two thirds of the argument — the clock cannot move an existing punch either, and
    an approved correction *does* change what the day reads without touching a byte
    of the row it corrects.
    """
    privileges = await platform.sql(
        "SELECT has_table_privilege('eam_app', 'attendance_events', 'SELECT'), "
        "has_table_privilege('eam_app', 'attendance_events', 'INSERT'), "
        "has_table_privilege('eam_app', 'attendance_events', 'UPDATE'), "
        "has_table_privilege('eam_app', 'attendance_events', 'DELETE')"
    )
    assert privileges == [(True, True, False, False)], (
        "the stream's privileges are not append-only: the runtime role may rewrite a punch"
    )

    await worked(platform, cast.subject.employee_id, start=9, end=CLOCKED_OUT)
    before = await event_rows(platform, cast.subject.employee_id)
    clocked_out = next(row for row in before if row[1] == "clock_out")

    # The clock is not a way to change one either: the same request is the same row,
    # and a different instant is a second punch the state machine refuses.
    async with attendance(platform) as service:
        replay = await service.clock(
            cast.subject.employee_id, EventType.CLOCK_OUT, at(MONDAY, CLOCKED_OUT), EventSource.WEB
        )
        with pytest.raises(DomainError) as refused:
            await service.clock(
                cast.subject.employee_id, EventType.CLOCK_OUT, at(MONDAY, 17), EventSource.WEB
            )
    assert replay.id == clocked_out[0]
    assert refused.value.code is AttendanceErrorCode.NO_OPEN_SHIFT

    # And the database refuses the edit itself, as the role the API connects with.
    async with app_connection() as session:
        with pytest.raises(Exception) as denial:
            await session.execute(
                text("UPDATE attendance_events SET occurred_at = now() WHERE id = :id"),
                {"id": clocked_out[0]},
            )
        await session.rollback()
    assert "permission denied" in str(denial.value).lower()

    filed_correction = await filed(cast.subject, at_hour=SHOULD_HAVE_BEEN)
    await fully_approved(platform, cast, filed_correction["id"])

    after = await event_rows(platform, cast.subject.employee_id)
    assert after[:2] == before, "the approved correction rewrote the row it corrects"
    assert await platform.scalar(
        "SELECT worked_minutes FROM attendance_daily WHERE employee_id = :id",
        {"id": cast.subject.employee_id},
    ) == 540, "an approved correction is what changes the day"


async def test_the_correction_document_cannot_be_deleted_by_the_runtime_role(
    platform: Platform, cast: Cast, app_connection: async_sessionmaker
) -> None:
    """The document that changed a punch is evidence, like the punch.

    The same treatment the stream, the day and the anomalies get: a correction is the
    reason a working-time figure is what it is, and the way to stop it being true is
    another document rather than the absence of this one.
    """
    drafted = await request_correction(cast.subject)
    assert drafted.status_code == 201, drafted.text

    async with app_connection() as session:
        with pytest.raises(Exception) as denial:
            await session.execute(text("DELETE FROM attendance_corrections"))
        await session.rollback()

    assert "permission denied" in str(denial.value).lower()
    assert await platform.scalar("SELECT count(*) FROM attendance_corrections") == 1


# --- HR's own corrections, down the same chain ----------------------------------


async def test_an_hr_correction_takes_the_same_chain(platform: Platform, cast: Cast) -> None:
    """事后修正 is the same document, the same two levels and the same append.

    HR names the employee, files, and the route resolves for *them* — their manager
    at level one and another HR member at level two — because the requester is who
    the engine resolves a route for. What arrives in the record is the same kind of
    row as an employee's correction, with HR's employee id on `created_by`, and the
    chain reads the same way on the subject's own screen.
    """
    await worked(platform, cast.subject.employee_id, start=9, end=CLOCKED_OUT)
    before = await event_rows(platform, cast.subject.employee_id)

    drafted = await request_correction(cast.hr, employee_id=cast.subject.employee_id)
    assert drafted.status_code == 201, drafted.text
    correction_id = drafted.json()["id"]
    assert (await cast.hr.post(
        f"/api/v1/attendance/corrections/{correction_id}/submit"
    )).status_code == 200

    at_manager = await approve(cast.manager, correction_id)
    assert at_manager.status_code == 200, at_manager.text
    at_hr = await approve(cast.other_hr, correction_id)
    assert at_hr.status_code == 200, at_hr.text
    assert at_hr.json()["state"] == CorrectionState.APPLIED.value

    rows = await event_rows(platform, cast.subject.employee_id)
    appended = [row for row in rows if row[1] == "correction"]
    assert len(appended) == 1
    assert appended[0][6] == UUID(cast.hr.employee_id), (
        "the appended row does not say who filed the correction"
    )
    assert appended[0][7] is not None, "HR's correction is an append like any other"
    assert rows[:2] == before, "HR's correction rewrote the punch it corrects"
    # The subject reads it as part of their own record, chain and all.
    read = await day_read(cast.subject)
    assert read.status_code == 200, read.text
    chain = read.json()["punches"][1]
    assert chain["is_corrected"] is True
    assert chain["corrections"][0]["reason"] == "I clocked out at 18:00, not 16:00"
    assert datetime.fromisoformat(chain["effective_at"]) == at(MONDAY, SHOULD_HAVE_BEEN)
