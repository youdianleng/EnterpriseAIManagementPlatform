"""Overtime: the pre-approval, the record, the smaller figure, and the monthly file.

No mocks and no in-memory repository, for the reason `test_leave.py` gives: most of what
this module has to get right is a *query* — which day a record is bucketed into, which
records a month's summary groups, what the attendance record says a day held — and a
substitute would answer those with the test's own assumptions.

**Two clocks, and each is used where it is the honest one.** The API tests run on the
real clock, so the dates they build are the module's own `madrid_today` — the same answer
the request path computes — and the pre-approval rule is asserted against a day that has
genuinely passed. The settlement tests inject a clock instead, because the rule they pin
("the smaller of the approved and the actually worked minutes, once the day is over") is
about a day that has ended and a working-time record that was punched months ago: a test
that waited for tonight to end is not a test.

**Both halves of every rule are asserted, because one of them is silent.** A settlement
that took the approved figure when the worked one was smaller looks exactly like a
settlement that took the smaller one until somebody reads a payroll file; an adjustment
that overwrote the computed value looks exactly like one that kept it. So each of those
is asserted from the side that goes wrong, on the value the database actually holds.

**The record's own arithmetic is asserted as *bytes*, not as a comparison.** The
computed figure is read back from the row before and after HR's adjustment and the two
are asserted equal as stored values: "kept beside, never over" is a statement about the
column, and a test that compared a re-derived figure would pass against an
implementation that had overwritten it.
"""

import csv
import io
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from uuid import UUID

import pytest

from app.core.errors import ErrorCode
from app.domain.approval.models import DecisionKind
from app.domain.approval.service import ApprovalService
from app.domain.attendance.business_day import MADRID, madrid_today
from app.domain.attendance.models import EventSource, EventType, utc_now
from app.domain.attendance.service import AttendanceService
from app.domain.notification.approval import ApprovalNotifier
from app.domain.notification.service import NotificationService
from app.domain.overtime.export import EXPORT_COLUMNS, EXPORT_EXCLUDES, TOTAL_LABEL
from app.domain.overtime.models import (
    MAX_DAY_MINUTES,
    OvertimeRequestState,
    month_bucket_of,
)
from app.domain.overtime.repository import OvertimeRepository
from app.domain.overtime.service import (
    DEFAULT_CONFIRMATION_THRESHOLD_MINUTES,
    OvertimeLedger,
    OvertimeService,
)
from app.domain.schedule.service import ScheduleService
from app.repositories.approval import PostgresApprovalRepository
from app.repositories.attendance import PostgresAttendanceRepository
from app.repositories.notification import PostgresNotificationRepository
from app.repositories.overtime import PostgresOvertimeRepository
from app.repositories.schedule import PostgresScheduleRepository
from tests.support.platform import Actor, Platform

#: Today, as the module itself answers it. The API computes this from the same clock and
#: the same tz database, so a request filed for `TODAY` in a test is filed for today in
#: the application — which is what lets the pre-approval rule be asserted without
#: freezing time.
TODAY = madrid_today(utc_now())
TOMORROW = TODAY + timedelta(days=1)
YESTERDAY = TODAY - timedelta(days=1)

#: The fixed month the settlement and export tests are about. March 2026 opens on a
#: Sunday, so the 2nd is a Monday and the 2nd to the 6th are five working days in a row —
#: a week of overtime somebody can count by hand.
MONTH = "2026-03"
MONDAY = date(2026, 3, 2)
TUESDAY = date(2026, 3, 3)
WEDNESDAY = date(2026, 3, 4)
THURSDAY = date(2026, 3, 5)
FRIDAY = date(2026, 3, 6)

#: The two clocks: the day the requests are filed (before the week) and the day the
#: sweep runs (after it). Both are Madrid instants, because that is the calendar every
#: date in this module is expressed in.
FILING_CLOCK = datetime(2026, 2, 25, 9, 0, tzinfo=MADRID)
SETTLING_CLOCK = datetime(2026, 3, 9, 9, 0, tzinfo=MADRID)

#: A shift, and what it is worth: 08:00 to 17:00 is nine hours, 540 minutes.
SHIFT_FROM = 8
SHIFT_TO = 17
SHIFT_MINUTES = 540

#: The threshold `config.Settings` ships, named here so a test that changes it says so.
THRESHOLD = DEFAULT_CONFIRMATION_THRESHOLD_MINUTES

#: A day far enough ahead that no clock a test uses has reached it: the record filed for
#: it is the one whose day is still open when the March sweep runs.
FAR_FUTURE = datetime(2027, 6, 1, tzinfo=MADRID).date()

#: The one reason this module stores, and it is about hours: the ticket's 事由.
REASON = "cierre de inventario"

#: Every route this surface publishes, and the whole of its write surface. Asserted as a
#: set rather than described in prose, because the ticket's first requirement is that one
#: of these does *not* exist: nothing here records overtime for a day that has passed.
EXPECTED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/v1/overtime/requests"),
        ("GET", "/api/v1/overtime/requests"),
        ("GET", "/api/v1/overtime/requests/{request_id}"),
        ("PATCH", "/api/v1/overtime/requests/{request_id}"),
        ("POST", "/api/v1/overtime/requests/{request_id}/submit"),
        ("POST", "/api/v1/overtime/requests/{request_id}/decide"),
        ("POST", "/api/v1/overtime/requests/{request_id}/withdraw"),
        ("GET", "/api/v1/overtime/records"),
        ("GET", "/api/v1/overtime/records/{record_id}"),
        ("POST", "/api/v1/overtime/records/{record_id}/confirm"),
        ("POST", "/api/v1/overtime/settlements"),
        ("GET", "/api/v1/overtime/summary"),
        ("GET", "/api/v1/overtime/export"),
    }
)


@dataclass
class Cast:
    """The people an overtime flow needs: a requester, an approver, HR, and finance."""

    subject: Actor
    manager: Actor
    hr: Actor
    finance: Actor
    #: Somebody else's report: the colleague who does *not* answer to `manager`.
    colleague: Actor
    other_manager: Actor


async def staff(platform: Platform, *, code: str = "ops") -> Cast:
    """A department, a position, a company week, and six accounts.

    The colleague is deliberately in the same department and answers to somebody else:
    "my department" is the reading of a manager's reach this module refuses, and a
    non-report in another department would pass for the wrong reason.
    """
    admin = await platform.admin()
    department = await platform.department(code)
    position = await platform.position(department, f"{code}-tech")
    created = await admin.post(
        "/api/v1/schedules",
        json={
            "code": f"{code}-week",
            "name_es": "Semana completa",
            "name_en": "Full week",
            "is_default": True,
            "days": [
                {
                    "weekday": weekday,
                    "expected_minutes": 480,
                    "start_time": "08:00",
                    "end_time": "16:00",
                }
                for weekday in range(5)
            ],
        },
    )
    assert created.status_code == 201, created.text

    manager = await platform.account(roles=("manager",))
    await platform.assign(manager.employee_id, department, position)

    other_manager = await platform.account(roles=("manager",))
    await platform.assign(other_manager.employee_id, department, position)

    subject = await platform.account(roles=("employee",))
    await platform.assign(
        subject.employee_id, department, position, manager_employee_id=manager.employee_id
    )

    hr = await platform.account(roles=("hr",))
    await platform.assign(hr.employee_id, department, position)

    finance = await platform.account(roles=("finance",))
    await platform.assign(finance.employee_id, department, position)

    colleague = await platform.account(roles=("employee",))
    await platform.assign(
        colleague.employee_id,
        department,
        position,
        manager_employee_id=other_manager.employee_id,
    )

    return Cast(
        subject=subject,
        manager=manager,
        hr=hr,
        finance=finance,
        colleague=colleague,
        other_manager=other_manager,
    )


@asynccontextmanager
async def overtime_service(
    platform: Platform, *, now=None, threshold_minutes: int = THRESHOLD
) -> AsyncIterator[OvertimeService]:
    """The module on its own session, wired exactly as the router wires it."""
    async with platform.factory() as session:
        overtime = PostgresOvertimeRepository(session)
        approvals = PostgresApprovalRepository(session)
        yield OvertimeService(
            overtime,
            session,
            approvals=ApprovalNotifier(
                engine=ApprovalService(approvals, session),
                notifications=NotificationService(
                    PostgresNotificationRepository(session), session
                ),
                approvals=approvals,
            ),
            attendance=AttendanceService(
                PostgresAttendanceRepository(session),
                expectations=ScheduleService(PostgresScheduleRepository(session), session),
                overtime=OvertimeLedger(overtime),
            ),
            threshold_minutes=threshold_minutes,
            now=now or utc_now,
        )


@asynccontextmanager
async def engine(platform: Platform) -> AsyncIterator[ApprovalService]:
    """The approval engine *alone*, to make a decision the overtime module never sees.

    That is the crash the resolve sweep exists for — the engine commits its own decision,
    so "approved" and "the record was written" are two moments — and this is how a test
    produces the state between them without breaking anything on purpose.
    """
    async with platform.factory() as session:
        yield ApprovalService(PostgresApprovalRepository(session), session)


async def worked(
    platform: Platform,
    employee_id: str,
    business_date: date,
    *,
    start: int = SHIFT_FROM,
    end: int = SHIFT_TO,
) -> None:
    """Punch a day, through the attendance module's own write path.

    Two punches on a Madrid day, which is what the settlement compares against. The
    instants carry Madrid's offset and are in the past, so the module accepts them — an
    offline punch synced later is a real punch — and the day's snapshot is written by the
    same call that appended them.
    """
    async with platform.factory() as session:
        punches = AttendanceService(
            PostgresAttendanceRepository(session),
            expectations=ScheduleService(PostgresScheduleRepository(session), session),
        )
        await punches.clock(
            UUID(employee_id),
            EventType.CLOCK_IN,
            datetime.combine(business_date, time(start), tzinfo=MADRID),
            EventSource.WEB,
        )
        await punches.clock(
            UUID(employee_id),
            EventType.CLOCK_OUT,
            datetime.combine(business_date, time(end), tzinfo=MADRID),
            EventSource.WEB,
        )


async def approved(
    service: OvertimeService,
    cast: Cast,
    *,
    business_date: date,
    expected_minutes: int,
    employee_id: str | None = None,
) -> UUID:
    """A request all the way through both levels: the day now has a record."""
    subject = UUID(employee_id or cast.subject.employee_id)
    view = await service.draft(
        employee_id=subject,
        business_date=business_date,
        expected_minutes=expected_minutes,
        reason=REASON,
    )
    await service.submit(view.request.id)
    await service.decide(
        view.request.id,
        approver_employee_id=UUID(cast.manager.employee_id),
        decision=DecisionKind.APPROVE,
    )
    decided = await service.decide(
        view.request.id,
        approver_employee_id=UUID(cast.hr.employee_id),
        decision=DecisionKind.APPROVE,
    )
    assert decided.record_id is not None, "an approval wrote no record"
    return decided.record_id


async def draft(actor: Actor, *, business_date: date, minutes: int = 120, **extra):
    body = {
        "business_date": business_date.isoformat(),
        "expected_minutes": minutes,
        "reason": REASON,
    }
    body.update(extra)
    return await actor.post("/api/v1/overtime/requests", json=body)


async def file_request(actor: Actor, request_id: str):
    return await actor.post(f"/api/v1/overtime/requests/{request_id}/submit")


async def decide(actor: Actor, request_id: str, *, decision: str = "approve"):
    return await actor.post(
        f"/api/v1/overtime/requests/{request_id}/decide", json={"decision": decision}
    )


async def approved_via_api(
    cast: Cast,
    *,
    business_date: date = TOMORROW,
    minutes: int = 120,
    requester: Actor | None = None,
    approver: Actor | None = None,
):
    """The same flow as `approved`, driven through the endpoints a client uses.

    `requester` and `approver` are named together because the engine's route is the
    reporting relationship: somebody else's request is decided by their own manager, and
    a test that approved a colleague's overtime with the wrong manager would be testing a
    route the engine refuses.
    """
    requester = requester or cast.subject
    approver = approver or (cast.manager if requester is cast.subject else cast.other_manager)
    created = await draft(requester, business_date=business_date, minutes=minutes)
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]
    filed = await file_request(requester, request_id)
    assert filed.status_code == 200, filed.text
    first = await decide(approver, request_id)
    assert first.status_code == 200, first.text
    second = await decide(cast.hr, request_id)
    assert second.status_code == 200, second.text
    assert second.json()["state"] == "approved", second.text
    return request_id, second.json()


def error_of(response) -> str:
    return response.json()["error"]["code"]


def rows_of(content: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(content)))


def records_of(response) -> list[dict]:
    assert response.status_code == 200, response.text
    return response.json()["items"]


# --- 1. the pre-approval, and the route that does not exist -------------------


def test_the_write_surface_has_no_retroactive_entry() -> None:
    """The ticket's first line, asserted against the published API rather than the prose.

    Every route this surface offers is listed here, read from the OpenAPI document — which
    is the surface a client sees, and the one a later endpoint would appear in. The
    assertions that matter are the two absences: there is no `POST /overtime/records` and
    no import or correction path, so a day of overtime cannot be created by anything but
    an approved request. A later ticket that added one would fail *here*, which is the
    point of enumerating rather than describing.
    """
    from app.main import app

    published = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/v1/overtime")
        for method in operations
    }

    assert published == set(EXPECTED_ROUTES)
    assert ("POST", "/api/v1/overtime/records") not in published
    # The record is reachable one way only: through the document that produced it.
    assert all(
        "records" not in path or method == "GET" or path.endswith("/confirm")
        for method, path in published
    )


async def test_overtime_for_a_day_that_has_passed_is_refused(platform: Platform) -> None:
    """事前申请, as a refusal: yesterday is not a day this module will take a request for.

    Both directions are asserted — yesterday is refused and today is accepted — because
    "the past is refused" and "everything is refused" look the same from one side, and a
    system nobody can file overtime in would pass half of this test.
    """
    cast = await staff(platform)

    late = await draft(cast.subject, business_date=YESTERDAY, minutes=120)

    assert late.status_code == 422, late.text
    assert error_of(late) == ErrorCode.OVERTIME_REQUEST_INVALID.value
    assert "in advance" in late.json()["error"]["detail"]
    assert await platform.scalar("SELECT count(*) FROM overtime_requests") == 0

    # The control: today itself is in advance of the overtime, and is accepted.
    on_time = await draft(cast.subject, business_date=TODAY, minutes=120)
    assert on_time.status_code == 201, on_time.text

    # And a draft cannot be edited back into the past either.
    request_id = on_time.json()["id"]
    edited = await cast.subject.patch(
        f"/api/v1/overtime/requests/{request_id}", json={"business_date": YESTERDAY.isoformat()}
    )
    assert edited.status_code == 422, edited.text
    stored = await platform.scalar(
        "SELECT business_date FROM overtime_requests WHERE id = :id", {"id": request_id}
    )
    assert stored == TODAY


async def test_only_an_approved_request_writes_a_record(platform: Platform) -> None:
    """Nothing, nothing, then one — at the second level and not before.

    The moments are asserted one at a time because each of them is a way the rule could
    be wrong: a record written when the draft was filed would count hours nobody agreed
    to, and one written at the first level would count them before HR saw them.
    """
    cast = await staff(platform)
    created = await draft(cast.subject, business_date=TOMORROW, minutes=120)
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]

    assert await platform.scalar("SELECT count(*) FROM overtime_records") == 0

    filed = await file_request(cast.subject, request_id)
    assert filed.status_code == 200, filed.text
    assert await platform.scalar("SELECT count(*) FROM overtime_records") == 0

    first = await decide(cast.manager, request_id)
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "in_approval"
    assert await platform.scalar("SELECT count(*) FROM overtime_records") == 0

    second = await decide(cast.hr, request_id)
    assert second.status_code == 200, second.text
    assert second.json()["state"] == "approved"
    assert second.json()["record_id"] is not None
    assert await platform.scalar("SELECT count(*) FROM overtime_records") == 1


async def test_a_rejected_request_writes_nothing_and_frees_the_day(platform: Platform) -> None:
    """A refusal is the engine's answer that there is no overtime, and the day reopens."""
    cast = await staff(platform)
    created = await draft(cast.subject, business_date=TOMORROW, minutes=120)
    request_id = created.json()["id"]
    await file_request(cast.subject, request_id)
    await decide(cast.manager, request_id)

    refused = await decide(cast.hr, request_id, decision="reject")

    assert refused.status_code == 200, refused.text
    assert refused.json()["state"] == "rejected"
    assert refused.json()["record_id"] is None
    assert await platform.scalar("SELECT count(*) FROM overtime_records") == 0

    # The day is free again: a rejected request must not block it for ever.
    again = await draft(cast.subject, business_date=TOMORROW, minutes=90)
    assert again.status_code == 201, again.text


async def test_one_request_per_person_per_day(platform: Platform) -> None:
    """Two intentions for one day would be two records, and a month counted twice."""
    cast = await staff(platform)
    first = await draft(cast.subject, business_date=TOMORROW, minutes=120)
    assert first.status_code == 201, first.text

    second = await draft(cast.subject, business_date=TOMORROW, minutes=60)

    assert second.status_code == 409, second.text
    assert error_of(second) == ErrorCode.OVERTIME_REQUEST_EXISTS.value

    # Somebody else's day is untouched by it, and so is the same person's next day.
    assert (
        await draft(cast.colleague, business_date=TOMORROW, minutes=60)
    ).status_code == 201
    assert (
        await draft(cast.subject, business_date=TOMORROW + timedelta(days=1), minutes=60)
    ).status_code == 201

    # And a day that already has a record is closed even to a new draft.
    request_id = first.json()["id"]
    await file_request(cast.subject, request_id)
    await decide(cast.manager, request_id)
    await decide(cast.hr, request_id)
    closed = await draft(cast.subject, business_date=TOMORROW, minutes=60)
    assert closed.status_code == 409, closed.text
    assert error_of(closed) == ErrorCode.OVERTIME_REQUEST_EXISTS.value


async def test_nobody_files_overtime_for_somebody_else(platform: Platform) -> None:
    """The self-only half: HR confirms a record, and nobody asks on a colleague's behalf."""
    cast = await staff(platform)

    for actor in (cast.manager, cast.hr, cast.finance):
        refused = await draft(
            actor, business_date=TOMORROW, minutes=120, employee_id=cast.subject.employee_id
        )
        assert refused.status_code == 403, refused.text
        assert error_of(refused) == ErrorCode.FORBIDDEN.value

    # The control: the same payload naming themselves is accepted.
    own = await draft(cast.hr, business_date=TOMORROW, minutes=120)
    assert own.status_code == 201, own.text


# --- 2. the record, and the month it is bucketed into -------------------------


async def test_an_approval_writes_the_record_into_its_month_bucket(platform: Platform) -> None:
    """`month_bucket` is the Madrid month of the day, and the summary is a group-by of it."""
    cast = await staff(platform)
    request_id, approved_request = await approved_via_api(cast, business_date=TOMORROW, minutes=90)

    stored = await platform.sql(
        """
        SELECT month_bucket, approved_minutes, worked_minutes, computed_minutes,
               needs_confirmation, settled_at
        FROM overtime_records WHERE employee_id = :id
        """,
        {"id": cast.subject.employee_id},
    )

    assert stored == [
        (month_bucket_of(TOMORROW), 90, None, None, False, None)
    ], "an approval should write the approved minutes and nothing else"
    # The bucket is the day's own month, not the month it was filed in: the two differ
    # for a request filed on the last day of a month for the first of the next.
    assert approved_request["record_id"] is not None
    assert await platform.scalar(
        "SELECT count(*) FROM overtime_entries WHERE entry_type = 'approve'"
    ) == 1

    # The document now points at the fact, and the month's summary groups it.
    read = await cast.subject.get(f"/api/v1/overtime/requests/{request_id}")
    assert read.json()["record_id"] == approved_request["record_id"]

    summary = await cast.subject.get(
        "/api/v1/overtime/summary", params={"month": month_bucket_of(TOMORROW)}
    )
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["month"] == month_bucket_of(TOMORROW)
    assert body["approved_minutes"] == 90
    assert body["awaiting_confirmation"] == 0
    assert [row["employee_id"] for row in body["items"]] == [cast.subject.employee_id]
    assert body["items"][0]["records"] == 1
    assert body["threshold_minutes"] == THRESHOLD


async def test_a_request_returned_for_correction_can_be_changed_and_filed_again(
    platform: Platform,
) -> None:
    """The engine's return is the module's draft, and the draft is the requester's to fix."""
    cast = await staff(platform)
    created = await draft(cast.subject, business_date=TOMORROW, minutes=120)
    request_id = created.json()["id"]
    await file_request(cast.subject, request_id)

    returned = await decide(cast.manager, request_id, decision="return")
    assert returned.status_code == 200, returned.text
    assert returned.json()["state"] == "draft"

    corrected = await cast.subject.patch(
        f"/api/v1/overtime/requests/{request_id}",
        json={"expected_minutes": 180, "reason": "cierre de inventario y recuento"},
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["expected_minutes"] == 180

    assert (await file_request(cast.subject, request_id)).status_code == 200
    assert (await decide(cast.manager, request_id)).status_code == 200
    final = await decide(cast.hr, request_id)
    assert final.json()["state"] == "approved"

    # What was approved is what the record states, not what the first draft said.
    assert await platform.scalar(
        "SELECT approved_minutes FROM overtime_records WHERE employee_id = :id",
        {"id": cast.subject.employee_id},
    ) == 180


async def test_a_filed_request_is_not_editable_and_an_approved_one_is_not_withdrawable(
    platform: Platform,
) -> None:
    """Two states, two refusals, and the alternative named for the second."""
    cast = await staff(platform)
    created = await draft(cast.subject, business_date=TOMORROW, minutes=120)
    request_id = created.json()["id"]
    await file_request(cast.subject, request_id)

    edited = await cast.subject.patch(
        f"/api/v1/overtime/requests/{request_id}", json={"expected_minutes": 60}
    )
    assert edited.status_code == 409, edited.text
    assert error_of(edited) == ErrorCode.OVERTIME_REQUEST_NOT_DRAFT.value

    # Withdrawing while it is in the queue is the requester's own act, and it works.
    stopped = await cast.subject.post(f"/api/v1/overtime/requests/{request_id}/withdraw")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["state"] == "withdrawn"
    assert await platform.scalar("SELECT count(*) FROM overtime_records") == 0

    # An approved one is a record, and the refusal sends the caller to HR's confirmation.
    _, approved_request = await approved_via_api(cast, business_date=TOMORROW, minutes=120)
    approved_id = approved_request["id"]
    refused = await cast.subject.post(f"/api/v1/overtime/requests/{approved_id}/withdraw")
    assert refused.status_code == 409, refused.text
    assert error_of(refused) == ErrorCode.OVERTIME_NOT_WITHDRAWABLE.value
    assert "confirms or adjusts" in refused.json()["error"]["detail"]
    assert await platform.scalar("SELECT count(*) FROM overtime_records") == 1


# --- 3. settlement: the smaller of the two figures ----------------------------


@pytest.fixture
async def settled_week(platform: Platform) -> tuple[Cast, dict[date, UUID]]:
    """A week of approved overtime, punched, settled, and what each day was about.

    The five days are the point of the fixture, because the rule has five shapes and
    each needs its own row: the approved figure smaller than the worked one, the worked
    one smaller, a difference inside the threshold, a difference exactly at it, and a day
    nobody worked at all.
    """
    cast = await staff(platform)

    async with overtime_service(platform, now=lambda: FILING_CLOCK) as service:
        shifts = {
            MONDAY: (120, SHIFT_MINUTES),
            TUESDAY: (600, SHIFT_MINUTES),
            WEDNESDAY: (560, SHIFT_MINUTES),
            THURSDAY: (570, SHIFT_MINUTES),
            FRIDAY: (120, 0),
        }
        records: dict[date, UUID] = {}
        for day, (minutes, _worked) in shifts.items():
            records[day] = await approved(
                service, cast, business_date=day, expected_minutes=minutes
            )

    for day, (_minutes, _worked) in shifts.items():
        if day != FRIDAY:
            await worked(platform, cast.subject.employee_id, day)

    async with overtime_service(platform, now=lambda: SETTLING_CLOCK) as service:
        report = await service.settle_due(month=MONTH)
        assert report.failed == (), report.failed
        assert report.settled_count == 5

    return cast, records


async def test_settlement_takes_the_smaller_of_the_approved_and_the_worked(
    platform: Platform, settled_week: tuple[Cast, dict[date, UUID]]
) -> None:
    """取较小值, both ways round, and the approved figure is never what got stored.

    The second row is the one that matters: 600 approved against 540 worked settles on
    540, so an implementation that took the approved figure whenever the worked one was
    smaller stores 600 and fails here — and the database refuses that row anyway, which
    is why the sweep's own report is asserted to have failed on nothing.
    """
    cast, _ = settled_week

    stored = await platform.sql(
        """
        SELECT business_date, approved_minutes, worked_minutes, computed_minutes
        FROM overtime_records WHERE employee_id = :id ORDER BY business_date
        """,
        {"id": cast.subject.employee_id},
    )

    assert stored == [
        # approved smaller than worked: the approved figure is the one that stands.
        (MONDAY, 120, 540, 120),
        # worked smaller than approved: the worked figure is the one that stands.
        (TUESDAY, 600, 540, 540),
        (WEDNESDAY, 560, 540, 540),
        (THURSDAY, 570, 540, 540),
        # Nobody punched: the approved overtime did not happen, and zero is the answer.
        (FRIDAY, 120, 0, 0),
    ], "settlement did not take the smaller of the two figures"


async def test_a_difference_beyond_the_threshold_is_marked_and_one_at_it_is_not(
    platform: Platform, settled_week: tuple[Cast, dict[date, UUID]]
) -> None:
    """A gap the company tolerates is settled quietly; a larger one waits for a person.

    The boundary is asserted on both sides — 20 minutes is inside the tolerance and 30 is
    exactly it, so neither is flagged — and so is the size of the flag's population,
    because a flag set on every record and one set on none are the same failure seen from
    two sides.
    """
    cast, _ = settled_week

    stored = await platform.sql(
        """
        SELECT business_date, needs_confirmation, confirmation_note
        FROM overtime_records WHERE employee_id = :id ORDER BY business_date
        """,
        {"id": cast.subject.employee_id},
    )

    assert stored == [
        # 420 apart: beyond the 30 the company allows.
        (MONDAY, True, None),
        # 60 apart, in the direction that costs somebody hours they worked.
        (TUESDAY, True, None),
        # 20 apart: inside the tolerance.
        (WEDNESDAY, False, None),
        # Exactly the threshold. "Exceeds" is strict, so this one is settled.
        (THURSDAY, False, None),
        # 120 apart: an approved shift nobody worked.
        (FRIDAY, True, None),
    ]

    queue = await cast.hr.get(
        "/api/v1/overtime/records",
        params={"employee_id": cast.subject.employee_id, "needs_confirmation": True},
    )
    assert [row["business_date"] for row in records_of(queue)] == [
        FRIDAY.isoformat(),
        TUESDAY.isoformat(),
        MONDAY.isoformat(),
    ]

    # The settle entry states why each flagged record is waiting, in the ledger.
    notes = await platform.sql(
        """
        SELECT note FROM overtime_entries
        WHERE entry_type = 'settle' AND note LIKE '%differ by%'
        ORDER BY seq
        """
    )
    assert len(notes) == 3
    assert all("beyond the 30 this company allows" in note for (note,) in notes)


async def test_the_threshold_is_a_setting(platform: Platform) -> None:
    """The same day, two tolerances, two answers: the number is configuration, not policy."""
    cast = await staff(platform)

    async with overtime_service(platform, now=lambda: FILING_CLOCK) as service:
        await approved(service, cast, business_date=MONDAY, expected_minutes=600)
    await worked(platform, cast.subject.employee_id, MONDAY, start=SHIFT_FROM, end=SHIFT_TO)

    async with overtime_service(
        platform, now=lambda: SETTLING_CLOCK, threshold_minutes=60
    ) as service:
        report = await service.settle_due(month=MONTH)
        assert report.failed == (), report.failed

    # 60 apart: flagged under the shipped 30, and inside a company that allows 60.
    assert await platform.scalar(
        "SELECT needs_confirmation FROM overtime_records WHERE employee_id = :id",
        {"id": cast.subject.employee_id},
    ) is False


async def test_settlement_does_not_run_before_the_day_is_over(platform: Platform) -> None:
    """A day whose hours are still being worked has no "actually worked" to compare.

    Left unsettled rather than settled against a partial total, and reported as skipped
    rather than as a failure — the day is not wrong, it is simply not finished.
    """
    cast = await staff(platform)

    async with overtime_service(platform, now=lambda: FILING_CLOCK) as service:
        await approved(service, cast, business_date=MONDAY, expected_minutes=120)
        # The sweep runs on the day itself.
        report = await service.settle_due(month=MONTH, on_date=MONDAY)

    assert report.settled_count == 0
    assert report.skipped == 1
    assert report.failed == ()
    assert await platform.sql(
        """
        SELECT computed_minutes, worked_minutes, settled_at, needs_confirmation
        FROM overtime_records WHERE employee_id = :id
        """,
        {"id": cast.subject.employee_id},
    ) == [(None, None, None, False)]


async def test_the_database_refuses_a_computed_figure_that_is_not_the_smaller(
    platform: Platform, settled_week: tuple[Cast, dict[date, UUID]]
) -> None:
    """The rule is a CHECK, not only a service method: a write that skipped it cannot land.

    The statement below is the mistake the ticket names — storing the approved figure on
    a day whose actual hours were fewer — and PostgreSQL refuses it by name.
    """
    cast, records = settled_week

    database_says = await platform.refused_by_database(
        "UPDATE overtime_records SET computed_minutes = approved_minutes WHERE id = :id",
        {"id": records[TUESDAY]},
    )

    assert "ck_overtime_records_smaller" in database_says


async def test_the_settle_sweep_is_idempotent(platform: Platform) -> None:
    """Running it twice settles nothing the second time, and moves no figure."""
    cast = await staff(platform)

    async with overtime_service(platform, now=lambda: FILING_CLOCK) as service:
        await approved(service, cast, business_date=MONDAY, expected_minutes=600)
    await worked(platform, cast.subject.employee_id, MONDAY)

    async with overtime_service(platform, now=lambda: SETTLING_CLOCK) as service:
        first = await service.settle_due(month=MONTH)
        second = await service.settle_due(month=MONTH)

    assert (first.settled_count, second.settled_count) == (1, 0)
    assert await platform.scalar(
        "SELECT count(*) FROM overtime_entries WHERE entry_type = 'settle'"
    ) == 1
    assert await platform.scalar(
        "SELECT computed_minutes FROM overtime_records WHERE employee_id = :id",
        {"id": cast.subject.employee_id},
    ) == SHIFT_MINUTES


# --- 4. HR's confirmation keeps the original ----------------------------------


async def test_hr_confirmation_keeps_the_computed_value_and_the_reason(
    platform: Platform,
) -> None:
    """The ticket's 保留原值与原因, asserted on the bytes the row holds.

    The computed figure is read back *before and after* the adjustment and the two are
    compared as stored values, because a test that re-derived it would pass against an
    implementation that had written over it. Everything the day was measured against —
    the approved minutes, the worked minutes, the computed minutes — is asserted
    unchanged, and HR's figure is asserted to sit beside them with the reason it differs.
    """
    cast = await staff(platform)

    async with overtime_service(platform, now=lambda: FILING_CLOCK) as service:
        await approved(service, cast, business_date=TUESDAY, expected_minutes=600)
    await worked(platform, cast.subject.employee_id, TUESDAY)

    async with overtime_service(platform, now=lambda: SETTLING_CLOCK) as service:
        await service.settle_due(month=MONTH)

    before = await platform.sql(
        """
        SELECT approved_minutes, worked_minutes, computed_minutes, needs_confirmation
        FROM overtime_records WHERE employee_id = :id
        """,
        {"id": cast.subject.employee_id},
    )
    assert before == [(600, SHIFT_MINUTES, SHIFT_MINUTES, True)]

    record_id = await platform.scalar(
        "SELECT id FROM overtime_records WHERE employee_id = :id",
        {"id": cast.subject.employee_id},
    )
    confirmed = await cast.hr.post(
        f"/api/v1/overtime/records/{record_id}/confirm",
        json={"minutes": 600, "note": "acordado con la responsable: se quedó a terminar"},
    )

    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    # HR's figure, and the three the day produced, still readable beside it.
    assert (body["confirmed_minutes"], body["effective_minutes"]) == (600, 600)
    assert (body["approved_minutes"], body["worked_minutes"], body["computed_minutes"]) == (
        600,
        SHIFT_MINUTES,
        SHIFT_MINUTES,
    )
    assert body["needs_confirmation"] is False
    assert body["confirmation_note"].startswith("acordado con la responsable")
    assert body["confirmed_by_employee_id"] == cast.hr.employee_id

    # Byte-identical: the same read again, straight from the table.
    after = await platform.sql(
        """
        SELECT approved_minutes, worked_minutes, computed_minutes, needs_confirmation
        FROM overtime_records WHERE employee_id = :id
        """,
        {"id": cast.subject.employee_id},
    )
    assert after[0][:3] == before[0][:3], "HR's adjustment overwrote what the day came to"
    assert after[0][3] is False, "the queue should be answered"

    # The ledger keeps every movement, in order, with the figures after each.
    history = [row["entry_type"] for row in body["history"]]
    assert history == ["approve", "settle", "confirm"]
    assert body["history"][1]["computed_minutes"] == SHIFT_MINUTES
    assert body["history"][2]["confirmed_minutes"] == 600
    assert body["history"][2]["note"] == body["confirmation_note"]
    assert body["history"][-1]["created_by_employee_id"] == cast.hr.employee_id


async def test_hr_may_adjust_below_the_computed_figure_and_the_ledger_says_so(
    platform: Platform,
) -> None:
    """An adjustment in the other direction is the same act, and it keeps the same original."""
    cast = await staff(platform)

    async with overtime_service(platform, now=lambda: FILING_CLOCK) as service:
        await approved(service, cast, business_date=MONDAY, expected_minutes=120)
    await worked(platform, cast.subject.employee_id, MONDAY)

    async with overtime_service(platform, now=lambda: SETTLING_CLOCK) as service:
        await service.settle_due(month=MONTH)

    record_id = await platform.scalar(
        "SELECT id FROM overtime_records WHERE employee_id = :id",
        {"id": cast.subject.employee_id},
    )
    confirmed = await cast.hr.post(
        f"/api/v1/overtime/records/{record_id}/confirm",
        json={"minutes": 60, "note": "se corrige el fichaje: salió antes"},
    )

    assert confirmed.status_code == 200, confirmed.text
    assert (
        confirmed.json()["computed_minutes"],
        confirmed.json()["approved_minutes"],
        confirmed.json()["worked_minutes"],
        confirmed.json()["confirmed_minutes"],
    ) == (120, 120, SHIFT_MINUTES, 60)
    assert await platform.scalar(
        """
        SELECT count(*) FROM overtime_entries
        WHERE entry_type = 'confirm' AND note LIKE '%salió antes%'
        """
    ) == 1


async def test_a_confirming_figure_needs_a_reason_and_a_settled_record(
    platform: Platform,
) -> None:
    """Two refusals: no reason stated, and nothing computed yet to keep."""
    cast = await staff(platform)

    async with overtime_service(platform, now=lambda: FILING_CLOCK) as service:
        record_id = await approved(service, cast, business_date=TOMORROW, expected_minutes=120)

    unsettled = await cast.hr.post(
        f"/api/v1/overtime/records/{record_id}/confirm",
        json={"minutes": 120, "note": "sin comparar"},
    )
    assert unsettled.status_code == 409, unsettled.text
    assert error_of(unsettled) == ErrorCode.OVERTIME_RECORD_NOT_SETTLED.value

    # The payload itself refuses a confirmation with no reason: this is the one act that
    # overrules a computed figure, and "why" is the whole of what makes it auditable.
    empty = await cast.hr.post(
        f"/api/v1/overtime/records/{record_id}/confirm",
        json={"minutes": 120, "note": ""},
    )
    assert empty.status_code == 422, empty.text


async def test_only_hr_confirms_and_no_role_confirms_for_themselves(
    platform: Platform, settled_week: tuple[Cast, dict[date, UUID]]
) -> None:
    """The adjustment is HR's alone: the manager who approved the overtime is refused it."""
    cast, records = settled_week
    record_id = records[MONDAY]

    for actor in (cast.subject, cast.manager, cast.finance, cast.other_manager):
        refused = await actor.post(
            f"/api/v1/overtime/records/{record_id}/confirm",
            json={"minutes": 120, "note": "quiero que cuente más"},
        )
        assert refused.status_code == 403, refused.text
        assert error_of(refused) == ErrorCode.FORBIDDEN.value

    allowed = await cast.hr.post(
        f"/api/v1/overtime/records/{record_id}/confirm",
        json={"minutes": 120, "note": "revisado"},
    )
    assert allowed.status_code == 200, allowed.text


async def test_the_ledger_is_append_only_for_the_runtime_role(platform: Platform) -> None:
    """The history is evidence, and evidence the role that serves requests can edit is not.

    The same guarantee the leave ledger carries, asserted at the same layer: the
    privileges the runtime role holds, and the documents and records it may not delete.
    """
    cast = await staff(platform)
    await approved_via_api(cast, business_date=TOMORROW, minutes=120)

    written = await platform.scalar(
        "SELECT count(*) FROM overtime_entries WHERE entry_type = 'approve'"
    )
    assert written == 1

    privileges = await platform.sql(
        """
        SELECT has_table_privilege('eam_app', 'overtime_entries', 'UPDATE'),
               has_table_privilege('eam_app', 'overtime_entries', 'DELETE'),
               has_table_privilege('eam_app', 'overtime_records', 'DELETE'),
               has_table_privilege('eam_app', 'overtime_requests', 'DELETE')
        """
    )
    assert tuple(privileges[0]) == (False, False, False, False)


# --- 5. the monthly export ----------------------------------------------------


async def exportable_month(platform: Platform, cast: Cast) -> list[tuple[str, str]]:
    """A month with three employees in it, settled and confirmed, and their staff numbers.

    Deliberately out of order and including somebody with no staff number at all: the
    file's order is by employee number and the one without a number sorts last, which is
    only visible if a test puts one there.
    """
    admin = await platform.admin()
    # Read as text: the ids travel into JSON payloads, and psycopg hands the columns back
    # as `UUID` objects that the request encoder will not serialise.
    department = str(
        await platform.scalar(
            "SELECT department_id FROM employee_assignments WHERE employee_id = :id",
            {"id": cast.subject.employee_id},
        )
    )
    position = str(
        await platform.scalar(
            "SELECT job_position_id FROM employee_assignments WHERE employee_id = :id",
            {"id": cast.subject.employee_id},
        )
    )

    people: list[tuple[str, str]] = []
    for employee_no, last_name, first_name in (
        ("E-0002", "Zubiri", "Ana"),
        ("E-0001", "Alonso", "Beto"),
    ):
        created = await admin.post(
            "/api/v1/employees",
            json={
                "first_name": first_name,
                "last_name": last_name,
                "email": f"{employee_no.lower()}@empresa.es",
                "hire_date": "2024-01-15",
                "private": {"employee_no": employee_no},
            },
        )
        assert created.status_code == 201, created.text
        employee_id = created.json()["id"]
        await platform.assign(
            employee_id, department, position, manager_employee_id=cast.manager.employee_id
        )
        people.append((employee_id, employee_no))

    async with overtime_service(platform, now=lambda: FILING_CLOCK) as service:
        # E-0002 works two days, E-0001 one, and the requester (no staff number) one.
        await approved(
            service, cast, business_date=MONDAY, expected_minutes=120, employee_id=people[0][0]
        )
        await approved(
            service, cast, business_date=WEDNESDAY, expected_minutes=90, employee_id=people[0][0]
        )
        await approved(
            service, cast, business_date=TUESDAY, expected_minutes=300, employee_id=people[1][0]
        )
        await approved(service, cast, business_date=THURSDAY, expected_minutes=60)

    for employee_id, _number in people:
        await worked(platform, employee_id, MONDAY)
    await worked(platform, people[0][0], WEDNESDAY)
    await worked(platform, people[1][0], TUESDAY)
    await worked(platform, cast.subject.employee_id, THURSDAY)

    async with overtime_service(platform, now=lambda: SETTLING_CLOCK) as service:
        report = await service.settle_due(month=MONTH)
        assert report.failed == (), report.failed
        assert report.settled_count == 4

    return people


async def test_the_month_is_a_file_of_minutes_and_one_total(platform: Platform) -> None:
    """Six columns, one row per record, ordered by employee then date, and one total line.

    E-0001 sorts before E-0002 however the rows were written, the two days of E-0002 are
    in date order, and the total line — a single one at the end, which is the choice this
    module documents — states the same sums a spreadsheet would compute from the rows
    above it.
    """
    cast = await staff(platform)
    await exportable_month(platform, cast)

    response = await cast.hr.get("/api/v1/overtime/export", params={"month": MONTH})

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert f'overtime-{MONTH}.csv' in response.headers["content-disposition"]
    rows = rows_of(response.text)
    assert rows[0] == list(EXPORT_COLUMNS)
    assert all(len(row) == len(EXPORT_COLUMNS) for row in rows)

    body = rows[1:]
    assert body[-1][0] == TOTAL_LABEL
    assert body[-1][1:] == ["", "", "", str(120 + 90 + 300 + 60), str(120 + 90 + 300 + 60)]
    lines = body[:-1]

    assert [line[0] for line in lines] == ["E-0001", "E-0002", "E-0002", ""]
    assert [line[3] for line in lines] == [
        TUESDAY.isoformat(),
        MONDAY.isoformat(),
        WEDNESDAY.isoformat(),
        THURSDAY.isoformat(),
    ]
    assert [line[1] for line in lines] == [
        "Alonso, Beto",
        "Zubiri, Ana",
        "Zubiri, Ana",
        "Lovelace, Ada",
    ]
    # The department, the approved minutes and the figure in force.
    assert lines[0][2] == "ops es"
    assert [line[4] for line in lines] == ["300", "120", "90", "60"]
    assert [line[5] for line in lines] == ["300", "120", "90", "60"]

    # One row per record, and one total: nothing else is in the file.
    assert await platform.scalar("SELECT count(*) FROM overtime_records") == 4


async def test_an_open_day_is_blank_in_the_file_rather_than_zero(platform: Platform) -> None:
    """A record whose day has not ended states no confirmed figure, and the total agrees.

    Empty rather than zero, the convention the attendance export established: zero is a
    figure the day produced and empty is "nobody has computed it yet".
    """
    cast = await staff(platform)
    await approved_via_api(cast, business_date=TOMORROW, minutes=120)

    response = await cast.hr.get(
        "/api/v1/overtime/export", params={"month": month_bucket_of(TOMORROW)}
    )

    rows = rows_of(response.text)
    assert rows[1][4] == "120", rows
    assert rows[1][5] == "", "an unsettled day stated a confirmed figure"
    assert rows[-1] == [TOTAL_LABEL, "", "", "", "120", ""]


async def test_the_file_states_minutes_and_no_amount(platform: Platform) -> None:
    """The ticket's 不含金额, asserted on the schema and on the header.

    Two layers, because the failure it prevents arrives twice: a column somebody adds to
    a table, and a header somebody adds to the file. Neither the three overtime tables
    nor the file's columns carry a rate, a multiplier, an amount or anything else that
    would make this module a second payroll record.
    """
    cast = await staff(platform)
    await approved_via_api(cast, business_date=TOMORROW, minutes=120)

    columns = await platform.sql(
        """
        SELECT table_name, column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name LIKE 'overtime%'
        """
    )
    assert {table for table, _ in columns} == {
        "overtime_requests",
        "overtime_records",
        "overtime_entries",
    }
    monetary = ("rate", "multiplier", "amount", "money", "salary", "cost", "pay", "price",
                "euro", "importe", "hourly", "wage", "total_")
    offenders = [
        f"{table}.{column}"
        for table, column in columns
        if any(word in column.lower() for word in monetary)
    ]
    assert offenders == [], f"the overtime schema gained a monetary column: {offenders}"

    for label in EXPORT_COLUMNS:
        assert not any(word in label.lower() for word in monetary), label
    assert EXPORT_EXCLUDES  # stated, not merely absent

    # And the file really has no such column, read as a client would read it.
    header = rows_of(
        (await cast.hr.get("/api/v1/overtime/export", params={"month": MONTH})).text
    )[0]
    assert header == list(EXPORT_COLUMNS)
    assert len(header) == 6


async def test_exporting_the_same_month_twice_leaves_two_records_and_one_file(
    platform: Platform,
) -> None:
    """A re-run is expected, allowed, and its own audit entry — the file is a report.

    The two responses are compared as bytes and the records behind them are compared
    before and after, because "finance re-ran the month" must not be a mutation that the
    second run then reports differently.
    """
    cast = await staff(platform)
    await exportable_month(platform, cast)

    reading = "SELECT computed_minutes, confirmed_minutes, needs_confirmation FROM overtime_records ORDER BY business_date"  # noqa: E501 - one statement, one line
    before = await platform.sql(reading)
    first = await cast.finance.get("/api/v1/overtime/export", params={"month": MONTH})
    second = await cast.finance.get("/api/v1/overtime/export", params={"month": MONTH})

    assert first.status_code == second.status_code == 200
    assert first.text == second.text, "the same month exported two different files"
    after = await platform.sql(reading)
    assert after == before, "exporting changed the records it states"

    trail = await platform.sql(
        """
        SELECT actor_user_id, after->>'period', after->>'records'
        FROM audit_log WHERE action = 'data.exported' ORDER BY id
        """
    )
    assert len(trail) == 2, "each export leaves its own record"
    assert {row[1] for row in trail} == {MONTH}
    assert {row[2] for row in trail} == {"4"}
    assert {str(row[0]) for row in trail} == {cast.finance.user_id}


async def test_hr_and_finance_export_and_nobody_else_does(platform: Platform) -> None:
    """§4.1's two halves: HR keeps the record, finance pays from it, and neither delegates.

    The file carries the staff number, which lives in the withheld block — the roles that
    may read that block include compliance, and compliance is deliberately refused here:
    the reader of the trail reads *who exported what*, not the payroll file.
    """
    cast = await staff(platform)

    for actor in (cast.hr, cast.finance):
        response = await actor.get("/api/v1/overtime/export", params={"month": MONTH})
        assert response.status_code == 200, response.text

    for actor in (cast.subject, cast.manager, cast.other_manager, cast.colleague):
        refused = await actor.get("/api/v1/overtime/export", params={"month": MONTH})
        assert refused.status_code == 403, refused.text
        assert error_of(refused) == ErrorCode.FORBIDDEN.value

    compliance = await platform.account(roles=("compliance",))
    refused = await compliance.get("/api/v1/overtime/export", params={"month": MONTH})
    assert refused.status_code == 403, refused.text


async def test_a_period_that_is_not_a_month_is_refused(platform: Platform) -> None:
    """The one argument the export takes is parsed once, in the module's own vocabulary."""
    cast = await staff(platform)

    for month in ("2026-13", "marzo", "2026", "2026-3"):
        response = await cast.hr.get("/api/v1/overtime/export", params={"month": month})
        assert response.status_code == 422, f"{month}: {response.text}"
        assert error_of(response) == ErrorCode.OVERTIME_PERIOD_INVALID.value

    summary = await cast.hr.get(
        "/api/v1/overtime/summary", params={"month": "2026-13", "everyone": True}
    )
    assert summary.status_code == 422
    assert error_of(summary) == ErrorCode.OVERTIME_PERIOD_INVALID.value


async def test_hr_settles_a_month_through_the_endpoint(platform: Platform) -> None:
    """The sweep is reachable, idempotent, and answers with what it did — including skips.

    Settlement cannot happen at approval for a day in the future, so this endpoint is how
    a month is finished; and running it twice must not write a second figure.
    """
    cast = await staff(platform)

    async with overtime_service(platform, now=lambda: FILING_CLOCK) as service:
        await approved(service, cast, business_date=MONDAY, expected_minutes=600)
    await worked(platform, cast.subject.employee_id, MONDAY)

    # A record whose day has not ended: filed for a day far enough ahead that the real
    # clock has not reached it, so the sweep leaves it alone rather than settling it
    # against a day nobody has worked yet.
    _, open_record = await approved_via_api(cast, business_date=FAR_FUTURE, minutes=120)

    # The endpoint runs on the real clock, so the March record's day is over and the
    # far-future one is not.
    first = await cast.hr.post("/api/v1/overtime/settlements", json={"month": MONTH})
    assert first.status_code == 200, first.text
    assert first.json() == {"settled": 1, "skipped": 0, "failed": []}
    assert await platform.scalar(
        """
        SELECT computed_minutes FROM overtime_records
        WHERE employee_id = :id AND business_date = :day
        """,
        {"id": cast.subject.employee_id, "day": MONDAY},
    ) == 540

    second = await cast.hr.post("/api/v1/overtime/settlements", json={"month": MONTH})
    assert second.json()["settled"] == 0

    # And HR's own record for the far future is still open, not settled at zero.
    assert open_record["record_id"] is not None
    assert await platform.scalar(
        "SELECT count(*) FROM overtime_records WHERE settled_at IS NULL"
    ) == 1

    for actor in (cast.subject, cast.manager, cast.finance):
        refused = await actor.post("/api/v1/overtime/settlements", json={"month": MONTH})
        assert refused.status_code == 403, refused.text


# --- 6. the day the overtime was worked on ------------------------------------


async def test_the_attendance_day_carries_the_days_approved_overtime(platform: Platform) -> None:
    """Ticket 26 fills `overtime_minutes`, and HR's confirmation moves it.

    The figure on the day is the one in force — the approved minutes until the day is
    settled, the smaller of the two after that, and HR's when they have confirmed it —
    which is the same value the monthly file states. The three moments are asserted one
    at a time, because a day that never followed the record would look right at every
    single one of them.
    """
    cast = await staff(platform)

    async with overtime_service(platform, now=lambda: FILING_CLOCK) as service:
        record_id = await approved(service, cast, business_date=MONDAY, expected_minutes=600)

    async def day_overtime() -> int | None:
        return await platform.scalar(
            """
            SELECT overtime_minutes FROM attendance_daily
            WHERE employee_id = :id AND business_date = :day
            """,
            {"id": cast.subject.employee_id, "day": MONDAY},
        )

    # Approved and not settled: the day carries what was agreed.
    assert await day_overtime() == 600

    await worked(platform, cast.subject.employee_id, MONDAY)
    async with overtime_service(platform, now=lambda: SETTLING_CLOCK) as service:
        await service.settle_due(month=MONTH)

    # Settled: 540 worked against 600 approved, so the day carries the smaller figure.
    assert await day_overtime() == 540

    await cast.hr.post(
        f"/api/v1/overtime/records/{record_id}/confirm",
        json={"minutes": 600, "note": "se quedó a terminar el inventario"},
    )

    # Confirmed: the day follows HR's figure, which is what the export states too.
    assert await day_overtime() == 600

    # And the employee's own day read shows it, which is the surface they use.
    day = await cast.subject.get(
        "/api/v1/attendance/day", params={"business_date": MONDAY.isoformat()}
    )
    assert day.status_code == 200, day.text
    assert day.json()["overtime_minutes"] == 600
    assert day.json()["worked_minutes"] == SHIFT_MINUTES

    # The record and the day are the same number, read from the two modules.
    assert await platform.scalar(
        """
        SELECT overtime_minutes = (
            SELECT COALESCE(confirmed_minutes, computed_minutes, approved_minutes)
            FROM overtime_records WHERE id = :record
        )
        FROM attendance_daily WHERE employee_id = :id AND business_date = :day
        """,
        {"id": cast.subject.employee_id, "day": MONDAY, "record": record_id},
    ) is True


async def test_a_day_with_no_approved_overtime_carries_nothing(platform: Platform) -> None:
    """Null, not zero: "nobody approved overtime" is a different answer from "it came to none".

    The distinction the column has carried since ticket 21, and the one this module could
    have broken by writing a zero on every day it touched.
    """
    cast = await staff(platform)
    await worked(platform, cast.subject.employee_id, MONDAY)

    day = await cast.subject.get(
        "/api/v1/attendance/day", params={"business_date": MONDAY.isoformat()}
    )

    assert day.status_code == 200, day.text
    assert day.json()["overtime_minutes"] is None
    assert day.json()["worked_minutes"] == SHIFT_MINUTES


async def test_the_attendance_module_without_an_overtime_source_still_works(
    platform: Platform,
) -> None:
    """The seam is optional, which is what keeps tickets 21 and 22's day reads unchanged.

    An `AttendanceService` built with no overtime source derives the same day it always
    did, with `overtime_minutes` null; nothing about the punch arithmetic, the expectation
    or the snapshot depends on this module existing.
    """
    cast = await staff(platform)
    await worked(platform, cast.subject.employee_id, MONDAY)

    async with platform.factory() as session:
        plain = AttendanceService(
            PostgresAttendanceRepository(session),
            expectations=ScheduleService(PostgresScheduleRepository(session), session),
        )
        day = await plain.day_view(UUID(cast.subject.employee_id), MONDAY)

    assert day.worked_minutes == SHIFT_MINUTES
    assert day.expected_minutes == 480
    assert day.overtime_minutes is None


async def test_the_range_question_answers_a_month_of_days_at_once(platform: Platform) -> None:
    """The seam's second half: a range read asks about every day in one query.

    The attendance range read derives a month of days that have no snapshot in one pass,
    so the ledger answers a whole range rather than a day at a time — and a day with no
    approved overtime is *absent* from the answer rather than present as zero, which is
    what keeps "no overtime was approved" different from "it came to nothing".
    """
    cast = await staff(platform)

    async with overtime_service(platform, now=lambda: FILING_CLOCK) as service:
        await approved(service, cast, business_date=MONDAY, expected_minutes=120)
        await approved(service, cast, business_date=WEDNESDAY, expected_minutes=90)

    async with platform.factory() as session:
        ledger = OvertimeLedger(PostgresOvertimeRepository(session))
        by_date = await ledger.overtime_by_date(
            UUID(cast.subject.employee_id), MONDAY, FRIDAY
        )

    assert by_date == {MONDAY: 120, WEDNESDAY: 90}


# --- 7. who sees whose overtime -----------------------------------------------


async def test_an_employee_reads_their_own_overtime_and_not_a_colleagues(
    platform: Platform,
) -> None:
    """The self-service read: the records, the totals and the detail behind them."""
    cast = await staff(platform)
    request_id, first = await approved_via_api(cast, business_date=TOMORROW, minutes=90)
    _, second = await approved_via_api(
        cast, business_date=TOMORROW + timedelta(days=1), minutes=60
    )

    own = records_of(await cast.subject.get("/api/v1/overtime/records"))
    assert {row["id"] for row in own} == {first["record_id"], second["record_id"]}
    # Newest day first, and every row is filed under the month its own day falls in.
    assert [row["business_date"] for row in own] == [
        (TOMORROW + timedelta(days=1)).isoformat(),
        TOMORROW.isoformat(),
    ]
    assert all(row["month_bucket"] == month_bucket_of(date.fromisoformat(row["business_date"]))
               for row in own)

    detail = await cast.subject.get(f"/api/v1/overtime/records/{first['record_id']}")
    assert detail.status_code == 200, detail.text
    assert [entry["entry_type"] for entry in detail.json()["history"]] == ["approve"]
    assert detail.json()["effective_minutes"] == 90

    # The document itself, and its approval history.
    read = await cast.subject.get(f"/api/v1/overtime/requests/{request_id}")
    assert read.json()["state"] == "approved"
    assert read.json()["approval"]["status"] == "approved"

    refused = await cast.subject.get(
        "/api/v1/overtime/records", params={"employee_id": cast.colleague.employee_id}
    )
    assert refused.status_code == 403, refused.text
    assert error_of(refused) == ErrorCode.FORBIDDEN.value
    assert "approved_minutes" not in refused.text, "the refusal carried the record it refused"


async def test_a_manager_reads_a_reports_overtime_and_is_refused_a_colleagues(
    platform: Platform,
) -> None:
    """The reporting relationship, not the department: both sit in the same team."""
    cast = await staff(platform)
    await approved_via_api(cast, business_date=TOMORROW, minutes=90)

    allowed = await cast.manager.get(
        "/api/v1/overtime/records", params={"employee_id": cast.subject.employee_id}
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["total"] == 1

    refused = await cast.manager.get(
        "/api/v1/overtime/records", params={"employee_id": cast.colleague.employee_id}
    )
    assert refused.status_code == 403, refused.text

    # The same reach over the totals and over one document.
    summary = await cast.manager.get(
        "/api/v1/overtime/summary",
        params={"month": month_bucket_of(TOMORROW), "employee_id": cast.subject.employee_id},
    )
    assert summary.status_code == 200, summary.text
    assert summary.json()["approved_minutes"] == 90

    refused_summary = await cast.manager.get(
        "/api/v1/overtime/summary",
        params={"month": month_bucket_of(TOMORROW), "employee_id": cast.colleague.employee_id},
    )
    assert refused_summary.status_code == 403, refused_summary.text


async def test_hr_and_finance_read_everybody_and_nobody_else_reads_the_company(
    platform: Platform,
) -> None:
    """§4.1 read into the catalogue: HR keeps the record, finance pays from it, and the
    company-wide read is those two — a manager's reach stops at their own reports."""
    cast = await staff(platform)
    await approved_via_api(cast, business_date=TOMORROW, minutes=90)
    await approved_via_api(
        cast, business_date=TOMORROW, minutes=60, requester=cast.colleague
    )
    month = month_bucket_of(TOMORROW)

    for actor in (cast.hr, cast.finance):
        everything = await actor.get(
            "/api/v1/overtime/summary", params={"month": month, "everyone": True}
        )
        assert everything.status_code == 200, everything.text
        assert everything.json()["approved_minutes"] == 150
        assert {row["employee_id"] for row in everything.json()["items"]} == {
            cast.subject.employee_id,
            cast.colleague.employee_id,
        }

    for actor in (cast.subject, cast.manager, cast.other_manager):
        refused = await actor.get(
            "/api/v1/overtime/summary", params={"month": month, "everyone": True}
        )
        assert refused.status_code == 403, refused.text
        assert error_of(refused) == ErrorCode.FORBIDDEN.value

    # An employee's own totals are theirs, without naming themselves.
    mine = await cast.subject.get("/api/v1/overtime/summary", params={"month": month})
    assert mine.status_code == 200, mine.text
    assert mine.json()["approved_minutes"] == 90
    assert [row["employee_id"] for row in mine.json()["items"]] == [cast.subject.employee_id]


async def test_an_unknown_record_is_a_404_for_anybody(platform: Platform) -> None:
    """Ids nobody wrote are answered before the permission is asked, so the route is not
    an existence oracle about other people's records."""
    cast = await staff(platform)

    missing = await cast.subject.get(f"/api/v1/overtime/records/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert error_of(missing) == ErrorCode.OVERTIME_RECORD_NOT_FOUND.value

    missing_request = await cast.hr.get(f"/api/v1/overtime/requests/{uuid.uuid4()}")
    assert missing_request.status_code == 404
    assert error_of(missing_request) == ErrorCode.OVERTIME_REQUEST_NOT_FOUND.value


# --- 8. the decision this module never saw ------------------------------------


async def test_the_resolve_sweep_writes_the_record_the_engine_already_made(
    platform: Platform,
) -> None:
    """The crash between "the engine approved" and "the record exists", caught up.

    The engine commits its own decision, so the gap is real; the sweep is what closes it,
    and it is idempotent — a second run finds the record already written and writes
    nothing.
    """
    cast = await staff(platform)
    created = await draft(cast.subject, business_date=TOMORROW, minutes=120)
    request_id = created.json()["id"]
    await file_request(cast.subject, request_id)
    await decide(cast.manager, request_id)

    # HR's approval taken straight to the engine, without this module noticing.
    async with engine(platform) as approvals:
        state = await approvals.state_of("overtime_request", UUID(request_id))
        assert state is not None
        await approvals.decide(state.id, UUID(cast.hr.employee_id), DecisionKind.APPROVE)

    assert await platform.scalar("SELECT count(*) FROM overtime_records") == 0

    async with overtime_service(platform) as service:
        report = await service.resolve_decided()

    assert report.resolved_count == 1
    assert report.failed == ()
    assert await platform.scalar(
        "SELECT approved_minutes FROM overtime_records WHERE employee_id = :id",
        {"id": cast.subject.employee_id},
    ) == 120
    assert await platform.scalar(
        "SELECT settled_at FROM overtime_requests WHERE id = :id", {"id": request_id}
    ) is not None, "the document should be finished with"

    async with overtime_service(platform) as service:
        again = await service.resolve_decided()
    assert again.resolved_count == 0
    assert await platform.scalar("SELECT count(*) FROM overtime_entries") == 1


def test_the_postgres_repository_answers_the_whole_protocol() -> None:
    """Structural conformance, asserted without a database.

    The service is written against the Protocol, so a method the repository forgot would
    otherwise be found by a request — or, worse, by the month-end settlement nobody is
    watching.
    """
    for name in (
        "save_request",
        "get_request",
        "list_requests",
        "count_requests",
        "write_draft",
        "mark_filed",
        "mark_approved",
        "mark_withdrawn",
        "mark_settled",
        "lock_next_unresolved",
        "approval_status_of",
        "live_request_for_day",
        "save_record",
        "get_record",
        "record_for_request",
        "record_for_day",
        "list_records",
        "count_records",
        "lock_next_unsettled",
        "write_settlement",
        "write_confirmation",
        "append_entry",
        "entries_for_record",
        "day_minutes",
        "minutes_by_date",
        "month_totals",
        "export_rows",
        "employee_exists",
        "commit",
        "rollback",
    ):
        assert hasattr(OvertimeRepository, name), f"{name} is not on the protocol"
        assert callable(getattr(PostgresOvertimeRepository, name, None)), (
            f"the repository does not answer {name}"
        )


def test_the_day_limit_is_a_days_worth_of_minutes() -> None:
    """The one bound this module shares with the table it writes."""
    assert MAX_DAY_MINUTES == 24 * 60
    assert OvertimeRequestState.APPROVED.value == "approved"
