"""Leave: the allowance, the working days, the reservation, and the calendar.

No mocks and no in-memory repository, for the reason `test_attendance_corrections.py`
gives: most of what this module has to get right is a *query* — which balance a year
holds, which dates the schedule calls working days, whether a request is in force on a
date — and a substitute would answer those with the test's own assumptions.

**The arithmetic is checked against a calendar somebody can count by hand.** March 2026
opens on a Sunday, so it holds twenty-two working days (five Mondays, five Tuesdays,
four of each of the rest), and the tests below assert 2, 4, 6 and 21 by counting on
paper first: a Friday-to-Monday request costs two days, a range across a Thursday
holiday and a weekend costs four, a request over the new year costs six split 4 + 2,
and the whole month with one holiday in it costs twenty-one. A test whose expected
value came out of the code under test proves nothing.

**Both directions of every rule are asserted, because half of them are silent.** A
reservation that is never released looks exactly like a request nobody filed, a
withdrawal refusal that never fires looks like a withdrawal that worked, and an
allowance check that is too strict looks like an employee with no days left — so each
of those is asserted from both sides below, including the boundary: a request for
exactly the remainder is accepted, and one day more is refused *with the remainder in
the message*, which is what the ticket asks a refusal to say.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

import pytest

from app.core.errors import ErrorCode
from app.domain.approval.models import DecisionKind
from app.domain.approval.service import ApprovalService
from app.domain.attendance.business_day import madrid_today
from app.domain.errors import DomainError
from app.domain.leave.repository import LeaveRepository
from app.domain.leave.service import LeaveService
from app.domain.notification.approval import ApprovalNotifier
from app.domain.notification.service import NotificationService
from app.domain.schedule.service import ScheduleService
from app.repositories.approval import PostgresApprovalRepository
from app.repositories.leave import PostgresLeaveRepository
from app.repositories.notification import PostgresNotificationRepository
from app.repositories.schedule import PostgresScheduleRepository
from tests.support.platform import Actor, Platform

#: March 2026, the month this module's arithmetic is asserted against. The 1st is a
#: Sunday, so the 2nd is the first Monday and the month holds twenty-two working days.
MARCH_MONDAY = date(2026, 3, 2)
MARCH_FRIDAY = date(2026, 3, 6)
NEXT_MONDAY = date(2026, 3, 9)
NEXT_TUESDAY = date(2026, 3, 10)
MARCH_LAST_DAY = date(2026, 3, 31)
MARCH_WORKING_DAYS = 22

#: A Thursday in the middle of the month, and a range that steps over it and a
#: weekend: Wednesday 18th to Tuesday 24th is 18, 20, 23 and 24 — four days.
MARCH_HOLIDAY = date(2026, 3, 19)
ACROSS_A_HOLIDAY = (date(2026, 3, 18), date(2026, 3, 24))
ACROSS_A_HOLIDAY_DAYS = 4

#: The new year: Monday 28 December 2026 to Tuesday 5 January 2027, with New Year's Day
#: (a Friday) as a holiday. December gives four days, January gives two.
CROSS_YEAR = (date(2026, 12, 28), date(2027, 1, 5))
NEW_YEAR_HOLIDAY = date(2027, 1, 1)
CROSS_YEAR_DECEMBER_DAYS = 4
CROSS_YEAR_JANUARY_DAYS = 2

#: A Monday and a Tuesday well clear of everything else, for the tests that need a
#: leave that has not started yet.
FUTURE_MONDAY = date(2027, 5, 3)
FUTURE_TUESDAY = date(2027, 5, 4)
FAR_MONDAY = date(2027, 6, 7)

#: The allowance `config.Settings` ships. The tests that change it build the service
#: themselves rather than editing the environment, which is what "configurable without
#: a code change" means: the figure is an argument.
DEFAULT_ALLOWANCE = 30

#: The one reference shape this module accepts: a storage key. Anything with a space or
#: an accent in it is content, and content is what §8 forbids.
SICK_NOTE = "sick-notes/2026/ana-0001.pdf"


@dataclass
class Cast:
    """The people a leave flow needs: a requester, their approver, and an HR member."""

    subject: Actor
    manager: Actor
    hr: Actor
    #: Somebody else's report: the colleague who does *not* answer to `manager`.
    colleague: Actor
    other_manager: Actor


async def staff(platform: Platform, *, code: str = "ops") -> Cast:
    """A department, a position, a company week, and five accounts.

    The schedule is the *company default* rather than the department's, so every
    employee in a test is on the same week and the arithmetic below is about dates
    rather than about who works where. The colleague is deliberately in the same
    department and answers to somebody else: "my department" is the reading of a
    manager's reach this ticket refuses, and a non-report in another department would
    pass for the wrong reason.
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
        colleague=colleague,
        other_manager=other_manager,
    )


async def holiday(platform: Platform, on_date: date, *, name: str = "Fiesta") -> None:
    """A national holiday, through the endpoint HR uses."""
    admin = await platform.admin()
    response = await admin.post(
        "/api/v1/holidays",
        json={
            "date": on_date.isoformat(),
            "name_es": name,
            "name_en": name,
            "scope": "national",
        },
    )
    assert response.status_code == 201, response.text


def next_monday(today: date) -> date:
    """The Monday after today, which is the soonest future working day of the week."""
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


@asynccontextmanager
async def leave_service(
    platform: Platform, *, annual_leave_days: int = DEFAULT_ALLOWANCE
) -> AsyncIterator[LeaveService]:
    """The module on its own session, wired as the router wires it."""
    async with platform.factory() as session:
        approvals = PostgresApprovalRepository(session)
        yield LeaveService(
            PostgresLeaveRepository(session),
            session,
            expectations=ScheduleService(PostgresScheduleRepository(session), session),
            approvals=ApprovalNotifier(
                engine=ApprovalService(approvals, session),
                notifications=NotificationService(
                    PostgresNotificationRepository(session), session
                ),
                approvals=approvals,
            ),
            annual_leave_days=annual_leave_days,
        )


@asynccontextmanager
async def engine(platform: Platform) -> AsyncIterator[ApprovalService]:
    """The approval engine *alone*, to make a decision the leave module never sees.

    That is the crash the settle sweep exists for — the engine commits its own
    decision, so "approved" and "the balance knows" are two moments — and this is how a
    test produces the state between them without breaking anything on purpose.
    """
    async with platform.factory() as session:
        yield ApprovalService(PostgresApprovalRepository(session), session)


# --- request helpers ----------------------------------------------------------


async def draft(actor: Actor, *, leave_type: str = "annual", start: date, end: date, **extra):
    body = {
        "leave_type": leave_type,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    body.update(extra)
    return await actor.post("/api/v1/leave/requests", json=body)


async def file_leave(
    actor: Actor, *, leave_type: str = "annual", start: date, end: date, **extra
):
    """A request that is drafted and filed, asserted to have worked."""
    created = await draft(actor, leave_type=leave_type, start=start, end=end, **extra)
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]
    filed = await actor.post(f"/api/v1/leave/requests/{request_id}/submit")
    assert filed.status_code == 200, filed.text
    return request_id, filed.json()


async def approve(actor: Actor, request_id: str, *, decision: str = "approve", comment=None):
    body: dict = {"decision": decision}
    if comment is not None:
        body["comment"] = comment
    return await actor.post(f"/api/v1/leave/requests/{request_id}/decide", json=body)


async def decide_correction(actor: Actor, correction_id: str, *, decision: str = "approve"):
    """The attendance flow's decision, which is a different document and a different route."""
    return await actor.post(
        f"/api/v1/attendance/corrections/{correction_id}/decide", json={"decision": decision}
    )


async def approved_leave(cast: Cast, *, start: date, end: date, leave_type: str = "annual"):
    """A request all the way through both levels: those days are now leave."""
    request_id, _ = await file_leave(cast.subject, leave_type=leave_type, start=start, end=end)
    first = await approve(cast.manager, request_id)
    assert first.status_code == 200, first.text
    second = await approve(cast.hr, request_id)
    assert second.status_code == 200, second.text
    assert second.json()["state"] == "approved", second.text
    return request_id


async def balances(actor: Actor, *, employee_id: str | None = None, year: int | None = None):
    params: dict = {}
    if employee_id is not None:
        params["employee_id"] = employee_id
    if year is not None:
        params["year"] = year
    response = await actor.get("/api/v1/leave/balances", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def balance_for(page: dict, code: str = "annual", *, year: int | None = None) -> dict:
    rows = [
        row
        for row in page["items"]
        if row["leave_type"] == code and (year is None or row["year"] == year)
    ]
    assert len(rows) == 1, f"expected one {code} balance for {year}, got {rows}"
    return rows[0]


async def annual_balance(actor: Actor, *, employee_id: str | None = None, year: int = 2026) -> dict:
    return balance_for(await balances(actor, employee_id=employee_id), "annual", year=year)


def error_of(response) -> str:
    return response.json()["error"]["code"]


# --- 1. the allowance is a parameter, and the API reports it -------------------


async def test_the_starter_catalogue_is_usable_without_anybody_writing_sql(
    platform: Platform,
) -> None:
    """The four Spanish shapes the migration seeds, with the flags that matter.

    `sick` is the one worth reading twice: it is paid, it wants the parte de baja as an
    attachment, and it does **not** spend the annual allowance — an installation cannot
    refuse somebody their statutory sick leave for want of days.
    """
    cast = await staff(platform)

    response = await cast.subject.get("/api/v1/leave/types")

    assert response.status_code == 200, response.text
    catalogue = {row["code"]: row for row in response.json()}
    assert set(catalogue) == {"annual", "sick", "personal", "parental"}
    assert catalogue["annual"]["counts_against_annual"] is True
    assert catalogue["annual"]["requires_attachment"] is False
    assert catalogue["sick"]["is_paid"] is True
    assert catalogue["sick"]["requires_attachment"] is True
    assert catalogue["sick"]["counts_against_annual"] is False
    assert catalogue["personal"]["is_paid"] is False
    assert catalogue["parental"]["counts_against_annual"] is False
    # Both languages: the interface ships in two, and a name missing from one renders
    # as nothing.
    assert catalogue["annual"]["name_es"] == "Vacaciones anuales"
    assert catalogue["annual"]["name_en"] == "Annual leave"


async def test_a_balance_reports_the_configured_allowance(platform: Platform) -> None:
    """The figure appears in the API that reports a balance, and the ledger says where.

    A year nobody has needed yet has no row, so the read answers with the allowance the
    parameter *would* grant — projected, with no id, and without writing anything — and
    the first filing materialises the row with a `grant` entry naming the parameter it
    came from.
    """
    cast = await staff(platform)

    before = await balances(cast.subject)

    assert before["annual_leave_days"] == DEFAULT_ALLOWANCE
    projected = balance_for(before, "annual", year=2026)
    assert (projected["id"], projected["projected"]) == (None, True)
    assert (projected["entitled_days"], projected["remaining_days"]) == (30, 30)
    assert await platform.scalar("SELECT count(*) FROM leave_balances") == 0, (
        "reading a balance created the ledger account as a side effect"
    )

    await file_leave(cast.subject, start=MARCH_MONDAY, end=MARCH_MONDAY)

    after = await annual_balance(cast.subject)
    assert after["projected"] is False and after["id"] is not None
    assert after["entitled_days"] == DEFAULT_ALLOWANCE
    assert [entry["entry_type"] for entry in after["history"]] == ["grant", "reserve"]
    assert after["history"][0]["note"] == "granted from annual_leave_days=30"
    assert after["history"][1]["days"] == 1


async def test_changing_the_parameter_changes_the_allowance_with_no_code_change(
    platform: Platform,
) -> None:
    """A different argument is a different allowance, down to the stored row.

    Built by hand rather than by editing the environment, because that *is* the claim:
    `annual_leave_days` is a value the service is constructed with, and nothing in the
    module knows the number 30.
    """
    cast = await staff(platform)

    async with leave_service(platform, annual_leave_days=25) as service:
        view = await service.draft(
            employee_id=uuid.UUID(cast.subject.employee_id),
            code="annual",
            start_date=MARCH_MONDAY,
            end_date=MARCH_MONDAY,
        )
        await service.submit(view.request.id)

    stored = await platform.scalar(
        "SELECT entitled_days FROM leave_balances WHERE employee_id = :id",
        {"id": cast.subject.employee_id},
    )
    page = await balances(cast.subject)

    assert stored == 25
    assert balance_for(page, "annual", year=2026)["entitled_days"] == 25
    granted = await platform.scalar(
        "SELECT note FROM leave_balance_entries WHERE entry_type = 'grant'"
    )
    assert granted == "granted from annual_leave_days=25"


# --- 2. the types are data -----------------------------------------------------


async def test_hr_retires_a_type_and_nothing_new_may_be_filed_under_it(
    platform: Platform,
) -> None:
    """Configured, not compiled in: adding a type and retiring one both work."""
    cast = await staff(platform)

    created = await cast.hr.post(
        "/api/v1/leave/types",
        json={
            "code": "study",
            "name_es": "Permiso de estudios",
            "name_en": "Study leave",
            "is_paid": True,
            "requires_attachment": False,
            "counts_against_annual": False,
        },
    )
    assert created.status_code == 201, created.text

    retired = await cast.hr.patch("/api/v1/leave/types/study", json={"is_active": False})
    assert retired.status_code == 200, retired.text
    assert retired.json()["is_active"] is False

    # A retired type is still readable — a request filed under it refers to it — and is
    # no longer offered, which is the difference between the two lists.
    offered = await cast.subject.get("/api/v1/leave/types")
    assert "study" not in {row["code"] for row in offered.json()}
    everything = await cast.subject.get(
        "/api/v1/leave/types", params={"include_inactive": True}
    )
    assert "study" in {row["code"] for row in everything.json()}

    refused = await draft(
        cast.subject, leave_type="study", start=MARCH_MONDAY, end=MARCH_MONDAY
    )
    assert refused.status_code == 422
    assert error_of(refused) == ErrorCode.LEAVE_TYPE_INACTIVE.value

    # And an employee may not maintain the catalogue.
    denied = await cast.subject.post(
        "/api/v1/leave/types",
        json={"code": "extra", "name_es": "Extra", "name_en": "Extra"},
    )
    assert denied.status_code == 403
    assert error_of(denied) == ErrorCode.FORBIDDEN.value


# --- 3. working days, from the schedule and the holiday calendar --------------


async def test_a_request_for_friday_to_monday_deducts_two_days(platform: Platform) -> None:
    """The weekend is not charged, and it is the schedule module that says so."""
    cast = await staff(platform)

    created = await draft(cast.subject, start=MARCH_FRIDAY, end=NEXT_MONDAY)

    assert created.status_code == 201, created.text
    assert created.json()["business_days_count"] == 2


async def test_a_holiday_inside_the_range_is_not_deducted(platform: Platform) -> None:
    """Hand-computed: Wed 18th to Tue 24th March 2026, with Thursday 19th a holiday.

    The range holds five weekdays and a weekend; the holiday takes one of the five, so
    the request costs 18, 20, 23 and 24 — four days.
    """
    cast = await staff(platform)
    await holiday(platform, MARCH_HOLIDAY, name="San José")

    created = await draft(cast.subject, start=ACROSS_A_HOLIDAY[0], end=ACROSS_A_HOLIDAY[1])

    assert created.status_code == 201, created.text
    assert created.json()["business_days_count"] == ACROSS_A_HOLIDAY_DAYS
    # The control: a week with no holiday in it costs all five of its days.
    without = await draft(
        cast.colleague, start=NEXT_MONDAY, end=date(2026, 3, 13)
    )
    assert without.json()["business_days_count"] == 5


async def test_a_whole_month_is_worth_twenty_two_working_days_and_one_holiday_less(
    platform: Platform,
) -> None:
    """The month-scale check: 22 by hand, 21 with one holiday in it.

    March 2026 has five Mondays, five Tuesdays and four of everything else, which is
    twenty-two working days. Both figures are asserted, because "the holiday was
    excluded" only means something beside the count that includes it.
    """
    cast = await staff(platform)
    month = (MARCH_MONDAY, MARCH_LAST_DAY)

    without = await draft(cast.subject, start=month[0], end=month[1])
    assert without.status_code == 201, without.text
    assert without.json()["business_days_count"] == MARCH_WORKING_DAYS

    await holiday(platform, MARCH_HOLIDAY, name="San José")
    with_holiday = await draft(cast.colleague, start=month[0], end=month[1])

    assert with_holiday.status_code == 201, with_holiday.text
    assert with_holiday.json()["business_days_count"] == MARCH_WORKING_DAYS - 1


async def test_a_range_with_no_working_day_is_refused_rather_than_costed_at_zero(
    platform: Platform,
) -> None:
    """A weekend is not a leave, and a leave that costs nothing is one no balance can
    account for."""
    cast = await staff(platform)

    saturday = date(2026, 3, 7)
    refused = await draft(cast.subject, start=saturday, end=saturday + timedelta(days=1))

    assert refused.status_code == 422
    assert error_of(refused) == ErrorCode.LEAVE_REQUEST_INVALID.value


# --- 4. the allowance, and the boundary ---------------------------------------


async def test_exactly_the_remaining_days_is_accepted_and_one_more_is_refused(
    platform: Platform,
) -> None:
    """The boundary, in both directions, with the remainder in the refusal.

    Six days granted. A five-day week is filed, then the sixth day is drafted while one
    day is still free and filed *because it is exactly what is left*, and a second
    one-day draft is taken while the same day was still free and is refused — naming the
    four figures, so whoever reads it does not have to work out which half was short.
    """
    cast = await staff(platform)
    set_to = await cast.hr.put(
        f"/api/v1/leave/balances/{cast.subject.employee_id}/2026/annual",
        json={"entitled_days": 6},
    )
    assert set_to.status_code == 200, set_to.text

    week = await file_leave(cast.subject, start=MARCH_MONDAY, end=MARCH_FRIDAY)
    assert week[1]["business_days_count"] == 5

    # Both one-day drafts are taken while the balance still has a day free: the draft
    # is a courtesy check, and the reservation is the decision.
    last_day = await draft(cast.subject, start=NEXT_MONDAY, end=NEXT_MONDAY)
    also_one_day = await draft(cast.subject, start=NEXT_TUESDAY, end=NEXT_TUESDAY)
    assert (last_day.status_code, also_one_day.status_code) == (201, 201)

    accepted = await cast.subject.post(
        f"/api/v1/leave/requests/{last_day.json()['id']}/submit"
    )
    assert accepted.status_code == 200, accepted.text

    page = await annual_balance(cast.subject)
    assert (page["pending_days"], page["remaining_days"]) == (6, 0)

    refused = await cast.subject.post(
        f"/api/v1/leave/requests/{also_one_day.json()['id']}/submit"
    )

    assert refused.status_code == 409
    assert error_of(refused) == ErrorCode.LEAVE_BALANCE_INSUFFICIENT.value
    detail = refused.json()["error"]["detail"]
    assert "remaining 0" in detail, detail
    assert "6 entitled + 0 carried over - 0 used - 6 pending" in detail, detail

    # Nothing moved: a refused filing is not a partial one.
    unchanged = await annual_balance(cast.subject)
    assert (unchanged["pending_days"], unchanged["remaining_days"]) == (6, 0)


async def test_a_draft_over_the_remaining_days_is_refused_while_it_is_still_a_draft(
    platform: Platform,
) -> None:
    """The courtesy layer: somebody learns before two approvers are asked."""
    cast = await staff(platform)
    await cast.hr.put(
        f"/api/v1/leave/balances/{cast.subject.employee_id}/2026/annual",
        json={"entitled_days": 1},
    )
    await file_leave(cast.subject, start=MARCH_MONDAY, end=MARCH_MONDAY)

    refused = await draft(cast.subject, start=NEXT_MONDAY, end=NEXT_MONDAY)

    assert refused.status_code == 409
    assert error_of(refused) == ErrorCode.LEAVE_BALANCE_INSUFFICIENT.value
    assert "remaining 0" in refused.json()["error"]["detail"]


async def test_a_type_that_spends_no_allowance_is_not_checked_against_one(
    platform: Platform,
) -> None:
    """Sick leave is recorded and filed like any other leave and costs no days.

    The control is the annual request beside it, which *is* refused once the year is
    spent — so "not checked" is a property of the type rather than of a balance that
    never filled up.
    """
    cast = await staff(platform)
    await cast.hr.put(
        f"/api/v1/leave/balances/{cast.subject.employee_id}/2026/annual",
        json={"entitled_days": 1},
    )
    await file_leave(cast.subject, start=MARCH_MONDAY, end=MARCH_MONDAY)

    sick = await file_leave(
        cast.subject,
        leave_type="sick",
        start=NEXT_MONDAY,
        end=date(2026, 3, 13),
        attachment_reference=SICK_NOTE,
    )
    assert sick[1]["business_days_count"] == 5

    over = await draft(cast.subject, start=NEXT_MONDAY, end=NEXT_MONDAY)
    assert over.status_code == 409

    # And no balance row exists for a type that has no allowance to hold.
    codes = {row["leave_type"] for row in (await balances(cast.subject))["items"]}
    assert "sick" not in codes


# --- 5. reserve, release, spend ------------------------------------------------


async def test_filing_reserves_the_days_and_an_approval_spends_them(
    platform: Platform,
) -> None:
    """`pending` while the engine has it, `used` once two levels have agreed."""
    cast = await staff(platform)

    request_id, filed = await file_leave(cast.subject, start=MARCH_FRIDAY, end=NEXT_MONDAY)
    assert filed["business_days_count"] == 2
    assert filed["state"] == "in_approval"

    reserved = await annual_balance(cast.subject)
    assert (reserved["pending_days"], reserved["used_days"], reserved["remaining_days"]) == (
        2,
        0,
        28,
    )

    assert (await approve(cast.manager, request_id)).status_code == 200
    middle = await annual_balance(cast.subject)
    assert (middle["pending_days"], middle["used_days"]) == (2, 0), (
        "level one approving does not spend anything: HR has not decided yet"
    )

    final = await approve(cast.hr, request_id)
    assert final.status_code == 200, final.text
    assert final.json()["state"] == "approved"

    spent = await annual_balance(cast.subject)
    assert (spent["pending_days"], spent["used_days"], spent["remaining_days"]) == (0, 2, 28)
    assert [entry["entry_type"] for entry in spent["history"]] == [
        "grant",
        "reserve",
        "consume",
    ]
    consumed = spent["history"][-1]
    assert (consumed["days"], consumed["pending_days"], consumed["used_days"]) == (2, 0, 2)


async def test_a_rejection_releases_the_reserved_days(platform: Platform) -> None:
    """The other direction: nothing was spent, and the year is whole again."""
    cast = await staff(platform)
    request_id, _ = await file_leave(cast.subject, start=MARCH_MONDAY, end=NEXT_MONDAY)

    refused = await approve(cast.manager, request_id, decision="reject", comment="busy week")
    assert refused.status_code == 200, refused.text
    assert refused.json()["state"] == "rejected"

    released = await annual_balance(cast.subject)
    assert (released["pending_days"], released["used_days"], released["remaining_days"]) == (
        0,
        0,
        30,
    )
    assert [entry["entry_type"] for entry in released["history"]] == [
        "grant",
        "reserve",
        "release",
    ]


async def test_a_withdrawal_releases_the_days_the_same_way(platform: Platform) -> None:
    """Withdrawing a filed request is the requester's release, and it is recorded."""
    cast = await staff(platform)
    request_id, _ = await file_leave(cast.subject, start=FUTURE_MONDAY, end=FUTURE_MONDAY)

    withdrawn = await cast.subject.post(f"/api/v1/leave/requests/{request_id}/withdraw")

    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["state"] == "withdrawn"
    released = await annual_balance(cast.subject, year=2027)
    assert (released["pending_days"], released["remaining_days"]) == (0, 30)
    assert released["history"][-1]["entry_type"] == "release"


async def test_an_approved_leave_withdrawn_before_it_starts_gives_the_days_back(
    platform: Platform,
) -> None:
    """A refund, which is a different movement from a release and is stored as one."""
    cast = await staff(platform)
    request_id = await approved_leave(cast, start=FUTURE_MONDAY, end=FUTURE_TUESDAY)

    spent = await annual_balance(cast.subject, year=2027)
    assert (spent["used_days"], spent["remaining_days"]) == (2, 28)

    refunded = await cast.subject.post(f"/api/v1/leave/requests/{request_id}/withdraw")

    assert refunded.status_code == 200, refunded.text
    assert refunded.json()["state"] == "withdrawn"
    back = await annual_balance(cast.subject, year=2027)
    assert (back["used_days"], back["pending_days"], back["remaining_days"]) == (0, 0, 30)
    assert back["history"][-1]["entry_type"] == "refund"


async def test_the_settle_sweep_finishes_a_settlement_the_engine_made_alone(
    platform: Platform,
) -> None:
    """The crash between the engine's commit and the balance's, and how it is repaired.

    The engine is asked directly, so the leave module never sees the decision: the days
    stay reserved and the leave is not yet in force. The sweep is what closes that gap,
    and running it twice moves nothing the second time.
    """
    cast = await staff(platform)
    request_id, _ = await file_leave(cast.subject, start=MARCH_MONDAY, end=MARCH_MONDAY)
    detail = await cast.subject.get(f"/api/v1/leave/requests/{request_id}")
    approval_request_id = UUID(detail.json()["approval"]["request_id"])

    async with engine(platform) as approvals:
        await approvals.decide(
            approval_request_id, UUID(cast.manager.employee_id), DecisionKind.APPROVE
        )
        await approvals.decide(
            approval_request_id, UUID(cast.hr.employee_id), DecisionKind.APPROVE
        )

    stranded = await annual_balance(cast.subject)
    assert (stranded["pending_days"], stranded["used_days"]) == (1, 0), (
        "the engine approved and the balance has not caught up: that is the gap"
    )

    async with leave_service(platform) as service:
        first = await service.settle_decided()
        second = await service.settle_decided()

    assert (first.settled_count, second.settled_count) == (1, 0)
    caught_up = await annual_balance(cast.subject)
    assert (caught_up["pending_days"], caught_up["used_days"]) == (0, 1)
    assert [entry["entry_type"] for entry in caught_up["history"]] == [
        "grant",
        "reserve",
        "consume",
    ]


# --- 6. cross-year -------------------------------------------------------------


async def test_a_request_over_the_new_year_is_charged_to_both_years(
    platform: Platform,
) -> None:
    """Six working days, four out of 2026 and two out of 2027.

    Hand-computed: Monday 28 to Thursday 31 December 2026 is four days, Friday 1 January
    2027 is a holiday, and Monday 4 and Tuesday 5 January are two. The second year has
    no balance row at all when the request is filed, which is the case worth pinning:
    the row is materialised from the parameter, with a `grant` entry and **nothing
    carried over** — carrying days is a decision, not a default.
    """
    cast = await staff(platform)
    await holiday(platform, NEW_YEAR_HOLIDAY, name="Año Nuevo")

    assert await platform.scalar("SELECT count(*) FROM leave_balances WHERE year = 2027") == 0

    request_id, filed = await file_leave(cast.subject, start=CROSS_YEAR[0], end=CROSS_YEAR[1])

    assert filed["business_days_count"] == CROSS_YEAR_DECEMBER_DAYS + CROSS_YEAR_JANUARY_DAYS
    assert [(item["year"], item["days"]) for item in filed["allocations"]] == [
        (2026, CROSS_YEAR_DECEMBER_DAYS),
        (2027, CROSS_YEAR_JANUARY_DAYS),
    ]

    page = await balances(cast.subject)
    december = balance_for(page, "annual", year=2026)
    january = balance_for(page, "annual", year=2027)
    assert (december["pending_days"], december["remaining_days"]) == (4, 26)
    assert (january["pending_days"], january["remaining_days"]) == (2, 28)
    assert january["carried_over_days"] == 0, (
        "days were carried into the new year without anybody deciding to"
    )
    assert [entry["entry_type"] for entry in january["history"]] == ["grant", "reserve"]
    assert january["history"][0]["days"] == DEFAULT_ALLOWANCE

    # The split survives the decision, and it is read back from the ledger rather than
    # recomputed from a calendar that may since have changed.
    await approve(cast.manager, request_id)
    decided = await approve(cast.hr, request_id)
    assert [(item["year"], item["days"]) for item in decided.json()["allocations"]] == [
        (2026, CROSS_YEAR_DECEMBER_DAYS),
        (2027, CROSS_YEAR_JANUARY_DAYS),
    ]
    after = await balances(cast.subject)
    assert balance_for(after, "annual", year=2026)["used_days"] == CROSS_YEAR_DECEMBER_DAYS
    assert balance_for(after, "annual", year=2027)["used_days"] == CROSS_YEAR_JANUARY_DAYS


async def test_a_cross_year_request_is_refused_when_the_second_year_cannot_cover_it(
    platform: Platform,
) -> None:
    """The refusal names the year that was short, and what that year had left.

    The balance is cut *between* the draft and the filing on purpose: that is the order
    the two checks run in, and the reservation is the one that decides. A person whose
    January had exactly one day left would have been told at the draft too, which is the
    courtesy layer the earlier test asserts.
    """
    cast = await staff(platform)
    await holiday(platform, NEW_YEAR_HOLIDAY, name="Año Nuevo")

    created = await draft(cast.subject, start=CROSS_YEAR[0], end=CROSS_YEAR[1])
    assert created.status_code == 201, created.text
    await cast.hr.put(
        f"/api/v1/leave/balances/{cast.subject.employee_id}/2027/annual",
        json={"entitled_days": 1},
    )
    refused = await cast.subject.post(f"/api/v1/leave/requests/{created.json()['id']}/submit")

    assert refused.status_code == 409
    detail = refused.json()["error"]["detail"]
    assert detail.startswith("2027 annual: 2 working day(s) requested"), detail
    assert "remaining 1" in detail, detail

    # The first year was not charged either: the whole filing rolled back, including
    # the row the attempt had materialised for 2026.
    rows = await platform.sql(
        "SELECT year, pending_days FROM leave_balances ORDER BY year"
    )
    assert rows == [(2027, 0)], rows


# --- 7. the calendar, and no anomaly on a day of leave ------------------------


async def test_an_approved_leave_day_produces_no_anomaly_and_the_same_day_without_it_does(
    platform: Platform,
) -> None:
    """The scan asks the leave module, and the answer is visible in what it records.

    Two employees on the same week, neither of whom punched anything: one has an
    approved leave on the Monday and the other has nothing. The scan runs through the
    *job's own factory*, so this is the production wiring rather than a service the test
    built — the same `LeaveCalendar` the correction flow and the nightly pass use.
    """
    from app.jobs.scan_attendance_anomalies import anomaly_service

    cast = await staff(platform)
    await approved_leave(cast, start=MARCH_MONDAY, end=MARCH_MONDAY)

    async with platform.factory() as session:
        report = await anomaly_service(session).scan(MARCH_MONDAY)

    assert report.examined >= 3, "the cast was due to work that Monday"
    rows = await platform.sql(
        "SELECT employee_id, type FROM attendance_anomalies WHERE business_date = :day",
        {"day": MARCH_MONDAY},
    )
    flagged = {str(employee_id) for employee_id, _type in rows}
    assert cast.subject.employee_id not in flagged, (
        "a day of approved leave was flagged as an absence"
    )
    assert cast.colleague.employee_id in flagged, (
        "the control: the same day without leave is a no_punches anomaly"
    )
    assert [row[1] for row in rows if str(row[0]) == cast.colleague.employee_id] == [
        "no_punches"
    ]


async def test_the_attendance_calendar_shows_the_approved_days_and_not_the_pending_ones(
    platform: Platform,
) -> None:
    """A filed leave is invisible on the calendar; an approved one is every day of it."""
    cast = await staff(platform)
    pending_id, _ = await file_leave(cast.subject, start=MARCH_MONDAY, end=MARCH_MONDAY)
    window = {"from_date": MARCH_FRIDAY.isoformat(), "to_date": NEXT_MONDAY.isoformat()}

    empty = await cast.subject.get("/api/v1/leave/calendar", params=window)
    assert empty.status_code == 200, empty.text
    assert empty.json()["days"] == [], "a request nobody has decided is not leave yet"

    await approve(cast.manager, pending_id)
    await approve(cast.hr, pending_id)
    # Friday to Monday, so the weekend is drawn too and the leave does not render as two.
    await approved_leave(cast, start=MARCH_FRIDAY, end=NEXT_MONDAY)
    shown = await cast.subject.get("/api/v1/leave/calendar", params=window)

    assert shown.status_code == 200, shown.text
    days = shown.json()["days"]
    assert [row["business_date"] for row in days] == [
        MARCH_FRIDAY.isoformat(),
        "2026-03-07",
        "2026-03-08",
        NEXT_MONDAY.isoformat(),
    ]
    assert {row["leave_type"] for row in days} == {"annual"}


async def test_a_correction_on_a_day_of_approved_leave_is_examined_against_the_leave(
    platform: Platform,
) -> None:
    """The correction flow builds the same calendar, and this is what proves it.

    Somebody who was on approved leave comes in for an hour and files a correction for
    the punch they forgot. With the leave lookup wired in, the re-examined day has
    nothing wrong with it; without it, a clock_out with no clock_in is a
    `missing_clock_in` anomaly — which is exactly what the day would be flagged as if
    the seam had been left answering `False`.
    """
    cast = await staff(platform)
    await approved_leave(cast, start=MARCH_MONDAY, end=MARCH_MONDAY)
    at = datetime.combine(MARCH_MONDAY, time(17, 0), tzinfo=UTC)

    created = await cast.subject.post(
        "/api/v1/attendance/corrections",
        json={
            "business_date": MARCH_MONDAY.isoformat(),
            "kind": "clock_out",
            "corrected_at": at.isoformat(),
            "reason": "Came in for an hour; forgot to clock out",
        },
    )
    assert created.status_code == 201, created.text
    correction_id = created.json()["id"]
    assert (
        await cast.subject.post(f"/api/v1/attendance/corrections/{correction_id}/submit")
    ).status_code == 200
    assert (await decide_correction(cast.manager, correction_id)).status_code == 200
    assert (await decide_correction(cast.hr, correction_id)).status_code == 200

    record = await cast.subject.get(
        "/api/v1/attendance/punches", params={"business_date": MARCH_MONDAY.isoformat()}
    )
    assert record.status_code == 200, record.text
    assert record.json()["anomalies"] == [], (
        "the re-examined day was judged without the leave calendar"
    )
    punched = await platform.sql(
        "SELECT count(*) FROM attendance_events WHERE business_date = :day",
        {"day": MARCH_MONDAY},
    )
    assert punched[0][0] == 1, "the control: the correction did append its punch"


# --- 8. withdrawal, in both directions ----------------------------------------


async def test_a_pending_request_may_be_withdrawn_at_any_time_before_it_starts(
    platform: Platform,
) -> None:
    """Months ahead, and the day before: both are before it starts."""
    cast = await staff(platform)
    soon = next_monday(madrid_today(datetime.now(UTC)))
    far, _ = await file_leave(cast.subject, start=FAR_MONDAY, end=FAR_MONDAY)
    near, _ = await file_leave(cast.subject, start=soon, end=soon)

    for request_id in (far, near):
        response = await cast.subject.post(f"/api/v1/leave/requests/{request_id}/withdraw")
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "withdrawn"

    assert (await annual_balance(cast.subject))["pending_days"] == 0

    # A withdrawn request is closed: there is nothing left to withdraw, and the refusal
    # says so rather than silently succeeding.
    again = await cast.subject.post(f"/api/v1/leave/requests/{far}/withdraw")
    assert again.status_code == 409
    assert error_of(again) == ErrorCode.LEAVE_NOT_WITHDRAWABLE.value


async def test_a_leave_that_has_begun_cannot_be_withdrawn_and_the_refusal_names_hr(
    platform: Platform,
) -> None:
    """The alternative is named, because "no" without a next step is not an answer.

    Both states are asserted — the request still waiting for a decision, and the one
    already approved — because a leave does not stop having begun when somebody says yes
    to it.
    """
    cast = await staff(platform)
    today = madrid_today(datetime.now(UTC))
    started = (today - timedelta(days=2), today + timedelta(days=2))
    # A second, earlier range: the two must not overlap, because a request over dates
    # a live one already covers is refused however far in the past it is.
    earlier = (today - timedelta(days=6), today - timedelta(days=4))

    pending, _ = await file_leave(cast.subject, start=started[0], end=started[1])
    refused = await cast.subject.post(f"/api/v1/leave/requests/{pending}/withdraw")

    assert refused.status_code == 409
    assert error_of(refused) == ErrorCode.LEAVE_ALREADY_STARTED.value
    assert "attendance correction flow" in refused.json()["error"]["detail"]

    approved = await approved_leave(cast, start=earlier[0], end=earlier[1])
    also_refused = await cast.subject.post(f"/api/v1/leave/requests/{approved}/withdraw")

    assert also_refused.status_code == 409
    assert error_of(also_refused) == ErrorCode.LEAVE_ALREADY_STARTED.value
    # And the days stay spent: a refused withdrawal is not a release.
    assert (await annual_balance(cast.subject))["used_days"] > 0


# --- 9. a sick note is a type, two dates, and a reference ---------------------


async def test_a_sick_leave_records_the_type_the_dates_and_a_reference_only(
    platform: Platform,
) -> None:
    """§8: the type and the dates. The file is elsewhere, and HR is who may read it."""
    cast = await staff(platform)

    missing = await draft(cast.subject, leave_type="sick", start=MARCH_MONDAY, end=MARCH_MONDAY)
    assert missing.status_code == 422
    assert error_of(missing) == ErrorCode.LEAVE_ATTACHMENT_REQUIRED.value

    filed = await file_leave(
        cast.subject,
        leave_type="sick",
        start=MARCH_MONDAY,
        end=MARCH_MONDAY,
        attachment_reference=SICK_NOTE,
    )
    assert filed[1]["has_attachment"] is True
    assert filed[1]["attachment_readable_by"] == ["hr"]
    assert filed[1]["attachment_reference"] is None, (
        "the employee's own read carries the fact that a file exists, not its key"
    )

    as_hr = await cast.hr.get(f"/api/v1/leave/requests/{filed[0]}")

    assert as_hr.status_code == 200, as_hr.text
    assert as_hr.json()["attachment_reference"] == SICK_NOTE
    stored = await platform.sql(
        "SELECT attachment_reference FROM leave_requests WHERE id = :id", {"id": filed[0]}
    )
    assert stored[0][0] == SICK_NOTE


async def test_a_reference_that_is_not_a_storage_key_is_refused(platform: Platform) -> None:
    """The one text field cannot hold a sentence, and the database agrees.

    Two layers, asserted separately: the service refuses it as a catalogued 422 — so the
    caller is told what the field is for — and PostgreSQL refuses it as well, so a write
    that skipped the service cannot put a diagnosis in the column either.
    """
    cast = await staff(platform)
    diagnosis = "baja por lumbalgia aguda, reposo 5 dias"

    refused = await draft(
        cast.subject,
        leave_type="sick",
        start=MARCH_MONDAY,
        end=MARCH_MONDAY,
        attachment_reference=diagnosis,
    )

    assert refused.status_code == 422
    assert error_of(refused) == ErrorCode.LEAVE_REQUEST_INVALID.value
    assert diagnosis not in refused.text, "the refusal echoed the content back"

    database_says = await platform.refused_by_database(
        """
        INSERT INTO leave_requests
            (id, employee_id, leave_type_id, start_date, end_date, business_days_count,
             attachment_reference)
        VALUES (gen_random_uuid(), :employee_id,
                (SELECT id FROM leave_types WHERE code = 'sick'),
                :start_date, :end_date, 1, :reference)
        """,
        {
            "employee_id": cast.subject.employee_id,
            "start_date": MARCH_MONDAY,
            "end_date": MARCH_MONDAY,
            "reference": diagnosis,
        },
    )

    assert "ck_leave_requests_attachment_reference" in database_says


async def test_the_request_schema_holds_no_free_text_and_no_medical_column(
    platform: Platform,
) -> None:
    """The ticket's §8 requirement, asserted against the table rather than the review.

    `leave_requests` has exactly one text column and it is a storage key. Nothing named
    for a reason, a diagnosis or an observation exists — which is what stops a later
    ticket adding one quietly, because this test is the place it would show up.

    The ledger's `note` is deliberately outside this assertion: it is prose about an
    *allowance* ("carried over from 2025"), written only by the HR action that changes a
    figure, and a balance has no medical content to leak.
    """
    columns = await platform.sql(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'leave_requests'
        """
    )
    text_columns = {
        name for name, kind in columns if kind in ("text", "character varying", "character")
    }
    assert text_columns == {"attachment_reference"}, (
        f"leave_requests gained a text column: {sorted(text_columns)}"
    )

    forbidden = ("reason", "diagnos", "medic", "symptom", "note", "comment", "detail", "descri")
    for table in ("leave_requests", "leave_types", "leave_balances"):
        names = await platform.sql(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = :table
            """,
            {"table": table},
        )
        offenders = [
            name for (name,) in names if any(word in name.lower() for word in forbidden)
        ]
        assert offenders == [], f"{table} carries free text: {offenders}"


async def test_a_request_payload_refuses_a_reason_field(platform: Platform) -> None:
    """The API has no field to write a diagnosis into, and says so rather than dropping it.

    `StrictModel` refuses unknown fields, so this is a 422 — the same answer the employee
    payload gives when somebody sends an ID number it does not hold.
    """
    cast = await staff(platform)

    refused = await cast.subject.post(
        "/api/v1/leave/requests",
        json={
            "leave_type": "sick",
            "start_date": MARCH_MONDAY.isoformat(),
            "end_date": MARCH_MONDAY.isoformat(),
            "attachment_reference": SICK_NOTE,
            "reason": "lumbalgia",
        },
    )

    assert refused.status_code == 422
    assert error_of(refused) == ErrorCode.VALIDATION_FAILED.value
    created = await platform.scalar(
        "SELECT count(*) FROM leave_requests WHERE employee_id = :id",
        {"id": cast.subject.employee_id},
    )
    assert created == 0


# --- 10. who sees whose leave -------------------------------------------------


async def test_an_employee_reads_their_own_balances_with_the_history_that_produced_them(
    platform: Platform,
) -> None:
    """The self-service read: what was granted, what moved, and what is left."""
    cast = await staff(platform)
    request_id, _ = await file_leave(cast.subject, start=MARCH_FRIDAY, end=NEXT_MONDAY)

    row = await annual_balance(cast.subject)

    assert row["remaining_days"] == 28
    assert [entry["entry_type"] for entry in row["history"]] == ["grant", "reserve"]
    assert row["history"][1]["remaining_days"] == 28
    assert row["history"][1]["leave_request_id"] == request_id

    requests = await cast.subject.get("/api/v1/leave/requests")
    assert requests.status_code == 200, requests.text
    assert [item["id"] for item in requests.json()["items"]] == [request_id]


async def test_a_manager_reads_a_reports_leave_and_is_refused_a_colleagues(
    platform: Platform,
) -> None:
    """The reporting relationship, not the department: both sit in the same team."""
    cast = await staff(platform)
    await file_leave(cast.subject, start=MARCH_MONDAY, end=MARCH_MONDAY)
    await file_leave(cast.colleague, start=MARCH_MONDAY, end=MARCH_MONDAY)

    allowed = await cast.manager.get(
        "/api/v1/leave/requests", params={"employee_id": cast.subject.employee_id}
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["total"] == 1

    refused = await cast.manager.get(
        "/api/v1/leave/requests", params={"employee_id": cast.colleague.employee_id}
    )
    assert refused.status_code == 403, refused.text
    assert error_of(refused) == ErrorCode.FORBIDDEN.value
    assert "business_days_count" not in refused.text, (
        "the refusal body carried the record it refused"
    )

    # The balances are governed by the same three actions.
    assert (
        await cast.manager.get(
            "/api/v1/leave/balances", params={"employee_id": cast.subject.employee_id}
        )
    ).status_code == 200
    assert (
        await cast.manager.get(
            "/api/v1/leave/balances", params={"employee_id": cast.colleague.employee_id}
        )
    ).status_code == 403


async def test_hr_reads_everybody_and_nobody_else_reads_the_company(
    platform: Platform,
) -> None:
    """`everyone=true` is HR's, and the refusal for the others is the same 403."""
    cast = await staff(platform)
    await file_leave(cast.subject, start=MARCH_MONDAY, end=MARCH_MONDAY)
    await file_leave(cast.colleague, start=MARCH_MONDAY, end=MARCH_MONDAY)

    everything = await cast.hr.get("/api/v1/leave/balances", params={"everyone": True})
    assert everything.status_code == 200, everything.text
    everyone = {row["employee_id"] for row in everything.json()["items"]}
    assert {cast.subject.employee_id, cast.colleague.employee_id} <= everyone

    for actor in (cast.subject, cast.manager):
        refused = await actor.get("/api/v1/leave/balances", params={"everyone": True})
        assert refused.status_code == 403, refused.text
        assert error_of(refused) == ErrorCode.FORBIDDEN.value

    # HR also reads one person's requests, which is the manager's reach widened to the
    # company rather than a different endpoint.
    one = await cast.hr.get(
        "/api/v1/leave/requests", params={"employee_id": cast.colleague.employee_id}
    )
    assert one.status_code == 200, one.text
    assert one.json()["total"] == 1


async def test_nobody_files_leave_on_somebody_elses_behalf(platform: Platform) -> None:
    """The self-only half: HR adjusts the allowance, the request is the employee's own."""
    cast = await staff(platform)

    for actor in (cast.manager, cast.hr):
        refused = await draft(
            actor, start=MARCH_MONDAY, end=MARCH_MONDAY, employee_id=cast.subject.employee_id
        )
        assert refused.status_code == 403, refused.text
        assert error_of(refused) == ErrorCode.FORBIDDEN.value

    # The control: the same payload naming themselves is accepted.
    own = await draft(cast.hr, start=MARCH_MONDAY, end=MARCH_MONDAY)
    assert own.status_code == 201, own.text


async def test_an_overlapping_request_is_refused(platform: Platform) -> None:
    """Two live requests over one day would be two deductions for one absence."""
    cast = await staff(platform)
    await file_leave(cast.subject, start=MARCH_MONDAY, end=MARCH_FRIDAY)

    overlapping = await draft(cast.subject, start=MARCH_FRIDAY, end=NEXT_MONDAY)

    assert overlapping.status_code == 409
    assert error_of(overlapping) == ErrorCode.LEAVE_REQUEST_OVERLAPS.value


async def test_hr_carries_days_over_and_the_history_says_so(platform: Platform) -> None:
    """The one figure only a person can write, and the ledger entries that record it."""
    cast = await staff(platform)

    granted = await cast.hr.put(
        f"/api/v1/leave/balances/{cast.subject.employee_id}/2026/annual",
        json={"entitled_days": 22, "carried_over_days": 5, "note": "Convenio: 22 + 5 de 2025"},
    )

    assert granted.status_code == 200, granted.text
    row = granted.json()
    assert (row["entitled_days"], row["carried_over_days"], row["remaining_days"]) == (22, 5, 27)
    assert [entry["entry_type"] for entry in row["history"]] == [
        "grant",
        "adjustment",
        "carry_over",
    ]
    assert row["history"][-1]["note"] == "Convenio: 22 + 5 de 2025"

    # And an employee may not set their own allowance.
    denied = await cast.subject.put(
        f"/api/v1/leave/balances/{cast.subject.employee_id}/2026/annual",
        json={"entitled_days": 300},
    )
    assert denied.status_code == 403
    assert error_of(denied) == ErrorCode.FORBIDDEN.value


async def test_the_runtime_role_cannot_rewrite_a_balance_history(platform: Platform) -> None:
    """The ledger is append-only in the database, not by convention.

    The same guarantee the expected-hours snapshot carries: "how was this balance
    computed" is evidence, and evidence the role that serves requests can edit is not.
    """
    cast = await staff(platform)
    await file_leave(cast.subject, start=MARCH_MONDAY, end=MARCH_MONDAY)

    written = await platform.scalar(
        "SELECT count(*) FROM leave_balance_entries WHERE entry_type = 'reserve'"
    )
    assert written == 1

    privileges = await platform.sql(
        """
        SELECT has_table_privilege('eam_app', 'leave_balance_entries', 'UPDATE'),
               has_table_privilege('eam_app', 'leave_balance_entries', 'DELETE'),
               has_table_privilege('eam_app', 'leave_requests', 'DELETE')
        """
    )
    assert tuple(privileges[0]) == (False, False, False)


async def test_the_balance_constraint_refuses_more_than_the_year_holds(
    platform: Platform,
) -> None:
    """The last line: even a write that skipped the service cannot overspend a year."""
    cast = await staff(platform)
    await file_leave(cast.subject, start=MARCH_MONDAY, end=MARCH_MONDAY)

    database_says = await platform.refused_by_database(
        """
        UPDATE leave_balances SET pending_days = 400
        WHERE employee_id = :employee_id AND year = 2026
        """,
        {"employee_id": cast.subject.employee_id},
    )

    assert "ck_leave_balances_within_allowance" in database_says


async def test_an_unknown_leave_type_and_an_inverted_range_are_refused(
    platform: Platform,
) -> None:
    """The two the caller can get wrong before anything else is asked."""
    cast = await staff(platform)

    unknown = await draft(
        cast.subject, leave_type="sabbatical", start=MARCH_MONDAY, end=MARCH_MONDAY
    )
    assert unknown.status_code == 404
    assert error_of(unknown) == ErrorCode.LEAVE_TYPE_NOT_FOUND.value

    inverted = await draft(cast.subject, start=NEXT_MONDAY, end=MARCH_MONDAY)
    assert inverted.status_code == 422
    assert error_of(inverted) == ErrorCode.LEAVE_REQUEST_INVALID.value


@pytest.mark.parametrize("code", ["annual", "sick"])
async def test_a_request_names_its_type_by_code_in_both_directions(
    platform: Platform, code: str
) -> None:
    """A type is addressed by its code everywhere: filing, reading, and the response."""
    cast = await staff(platform)
    extra = {"attachment_reference": SICK_NOTE} if code == "sick" else {}

    request_id, filed = await file_leave(
        cast.subject, leave_type=code, start=MARCH_MONDAY, end=MARCH_MONDAY, **extra
    )

    assert filed["leave_type"] == code
    read = await cast.subject.get(f"/api/v1/leave/requests/{request_id}")
    assert read.status_code == 200, read.text
    assert read.json()["leave_type"] == code


async def test_two_submissions_racing_for_the_last_day_leave_one_of_them_refused(
    platform: Platform,
) -> None:
    """The lock, not the check: two filings in flight at once cannot both take the day.

    Both drafts are written while a day is still free, and then filed concurrently on
    their own sessions. `SELECT ... FOR UPDATE` on the balance row is what serialises
    them, so one reserves the day and the other is refused with the remainder — a
    check-then-write without the lock would let both read "1 remaining" and both write.
    """
    cast = await staff(platform)
    await cast.hr.put(
        f"/api/v1/leave/balances/{cast.subject.employee_id}/2026/annual",
        json={"entitled_days": 1},
    )
    first = await draft(cast.subject, start=MARCH_MONDAY, end=MARCH_MONDAY)
    second = await draft(cast.subject, start=NEXT_MONDAY, end=NEXT_MONDAY)
    assert (first.status_code, second.status_code) == (201, 201)

    async def file_it(request_id: str) -> str:
        async with leave_service(platform) as service:
            try:
                await service.submit(uuid.UUID(request_id))
            except DomainError as error:
                return error.code.value
        return "submitted"

    outcomes = await asyncio.gather(
        file_it(first.json()["id"]), file_it(second.json()["id"])
    )

    assert sorted(outcomes) == [
        ErrorCode.LEAVE_BALANCE_INSUFFICIENT.value,
        "submitted",
    ], outcomes
    page = await annual_balance(cast.subject)
    assert (page["pending_days"], page["remaining_days"]) == (1, 0)


def test_the_postgres_repository_answers_the_whole_protocol() -> None:
    """Structural conformance, asserted without a database.

    The service is written against the Protocol, so a method the repository forgot would
    otherwise be found by a request — or, worse, by the nightly scan on a night nobody is
    watching.
    """
    for name in (
        "list_types",
        "get_type_by_code",
        "get_type",
        "get_balance",
        "get_balance_by_id",
        "ensure_balance",
        "lock_balance",
        "write_balance",
        "append_entry",
        "entries_for_request",
        "entries_for_balance",
        "save_request",
        "get_request",
        "list_requests",
        "count_requests",
        "mark_filed",
        "mark_approved",
        "mark_withdrawn",
        "mark_settled",
        "lock_next_unsettled",
        "approval_status_of",
        "live_request_overlapping",
        "approved_requests_covering",
        "employee_exists",
        "commit",
        "rollback",
    ):
        assert hasattr(LeaveRepository, name), f"{name} is not on the protocol"
        assert callable(getattr(PostgresLeaveRepository, name, None)), (
            f"the repository does not answer {name}"
        )
