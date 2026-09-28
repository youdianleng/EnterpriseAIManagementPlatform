"""Who a manager's permission snapshot says they reach, in both directions.

`Principal.reports_employee_ids` is the relationship the kernel's
`MANAGER_OF_SUBJECT` clause tests and the field `filter_for` hands a store, so what
ends up in it decides how much of four surfaces a manager sees: the hours report,
leave, overtime and attendance corrections. It is derived in one place rather than
by a query in each of those modules, which is exactly why it needs a test of its
own: the four surfaces cannot each be the place a wrong set is noticed.

**The second test exists because the builder was wrong.** It unioned the approver of
the caller's *own* assignment into the same set, so a manager's "reports" contained
their own boss. The kernel then granted the manager their manager's hours, leave and
overtime, and the daily digest addressed them their boss's activity. The field's own
docstring said one direction; the code did two.

Both tests run on the restricted role requests use, because that is the connection
the snapshot is built on in production: a builder whose query `eam_app` may not run
would pass a test written against the owner and fail on every request.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain.access.kernel import Action, Reason, Resource, ResourceKind, can
from app.domain.access.snapshot import PrincipalBuilder, resolve_principal
from tests.support.platform import Platform


@pytest.fixture
async def app_connection(settings) -> AsyncIterator[async_sessionmaker]:
    """Sessions bound to the role requests use, on the test database."""
    engine = create_async_engine(settings.runtime_test_database_url)
    try:
        yield async_sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@dataclass(slots=True, frozen=True)
class Chain:
    """A manager with one report below them and their own approver above.

    The middle person is the interesting one, so both of their identifiers are kept:
    `manager_employee_id` is what the reports query is asked about, and
    `manager_user_id` is what a request would resolve a principal from.
    """

    boss_employee_id: str
    manager_user_id: str
    manager_employee_id: str
    report_employee_id: str


async def seed_chain(platform: Platform) -> Chain:
    """Three accounts and the two assignments that relate them, as an admin would.

    Written through the API rather than by hand because the relationship under test
    is the one the employee module writes: a test that inserted the rows itself would
    agree with whatever it inserted and not with what production records. The held
    position is managerial, which is where the `manager` role comes from.
    """
    department = await platform.department("OPS")
    position = await platform.position(department, "OPS-LEAD", is_managerial=True)
    boss = await platform.grant_account(sign_in=False)
    manager = await platform.grant_account(sign_in=False)
    report = await platform.grant_account(sign_in=False)
    await platform.assign(boss.employee_id, department, position)
    await platform.assign(
        manager.employee_id, department, position, manager_employee_id=boss.employee_id
    )
    await platform.assign(
        report.employee_id, department, position, manager_employee_id=manager.employee_id
    )
    return Chain(
        boss_employee_id=boss.employee_id,
        manager_user_id=manager.user_id,
        manager_employee_id=manager.employee_id,
        report_employee_id=report.employee_id,
    )


async def test_the_reports_set_runs_one_way(platform: Platform, app_connection) -> None:
    """The people whose assignment names the caller, and nobody else.

    Three assertions and each of them is a different bug: the report must be in, the
    caller's own approver must be out, and the caller must not be their own report —
    the last because a row that named the person as their own manager would put them
    in the set, and "I report to myself" reads as permission everywhere it is used.
    """
    chain = await seed_chain(platform)

    async with app_connection() as session:
        _departments, _primary, is_manager, reports = await PrincipalBuilder(
            session
        ).assignment_facts(UUID(chain.manager_employee_id))

    assert is_manager is True, "the held position is managerial, so the role is derived"
    assert UUID(chain.report_employee_id) in reports, "a direct report is missing"
    assert UUID(chain.boss_employee_id) not in reports, (
        "a manager's reports contain their own approver: the upward reach is back, "
        "and with it the hours, leave and overtime of their own boss"
    )
    assert UUID(chain.manager_employee_id) not in reports, "the caller was made their own report"


async def test_a_manager_is_refused_their_own_approvers_records(
    platform: Platform, app_connection
) -> None:
    """The same fact as the kernel sees it, through the snapshot a request resolves.

    `resolve_principal` rather than the builder directly, because the wrong set was
    only harmful once it reached a decision: this asserts what a live request would be
    told, including the reason, so a change that widened the set again would fail here
    rather than only in the builder's own test.
    """
    chain = await seed_chain(platform)

    async with app_connection() as session:
        principal = await resolve_principal(session, UUID(chain.manager_user_id))

    assert principal is not None
    assert "manager" in principal.roles, "the managerial position did not derive the role"

    their_report = Resource(
        ResourceKind.TIMESHEET_REPORT, owner_employee_id=UUID(chain.report_employee_id)
    )
    their_approver = Resource(
        ResourceKind.TIMESHEET_REPORT, owner_employee_id=UUID(chain.boss_employee_id)
    )

    assert can(principal, Action.TIMESHEET_READ_REPORT, their_report).allowed
    refused = can(principal, Action.TIMESHEET_READ_REPORT, their_approver)
    assert refused.denied, f"a manager read their own approver's records: {refused.detail}"
    assert refused.primary_reason is Reason.NOT_YOUR_TIMESHEET_SCOPE
