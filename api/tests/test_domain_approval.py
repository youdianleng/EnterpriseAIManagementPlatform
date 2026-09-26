"""The approval engine, driven over a real database.

No mocks and no in-memory repository: half of what this engine has to get right is
a *query* — whose primary position is whose, which department manager to fall back
to, who holds `hr` — and a substitute would answer those with the test's own
assumptions. `tests/support/platform.py` supplies committed employees, departments,
assignments and accounts, which is what these rules are about.

**Every operation runs on a session of its own**, the way a request does, and
commits before the block ends. Nothing asserted below can therefore have come from
an object the service kept in memory: it came back out of PostgreSQL.

The route under test is the one the service documents: level 1 is the requester's
primary-position approver, falling back to that department's manager; level 2 is
any holder of the `hr` role other than the requester; and nobody decides their own
request.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.domain.approval.errors import ApprovalErrorCode
from app.domain.approval.models import (
    OPEN_STATUSES,
    SELF_APPROVAL_REASON,
    ApprovalState,
    ApprovalStatus,
    DecisionKind,
    StepStatus,
    SubmitContext,
)
from app.domain.approval.service import ApprovalService
from app.domain.errors import DomainError
from app.repositories.approval import PostgresApprovalRepository
from tests.support.platform import Platform

#: The role requests connect as, named here rather than imported from the
#: migration so a rename shows up as a failing test rather than as two places
#: agreeing on the new name.
APP_ROLE = "eam_app"

#: An entity type with no table, no model and no code anywhere. The engine stores
#: it and moves it; that is the whole point (see the last test).
ENTITY = "leave_request"


@asynccontextmanager
async def engine(platform: Platform) -> AsyncIterator[ApprovalService]:
    """The engine on its own session, the way one request uses it."""
    async with platform.factory() as session:
        yield ApprovalService(PostgresApprovalRepository(session), session)


async def submit(
    platform: Platform,
    entity_id: UUID,
    requester: UUID,
    *,
    entity_type: str = ENTITY,
    context: SubmitContext | None = None,
) -> UUID:
    async with engine(platform) as approvals:
        return await approvals.submit(entity_type, entity_id, requester, context)


async def decide(
    platform: Platform,
    request_id: UUID,
    approver: UUID,
    decision: DecisionKind,
    comment: str | None = None,
    *,
    actor_user_id: UUID | None = None,
    actor_roles: frozenset[str] | None = None,
) -> ApprovalState:
    async with engine(platform) as approvals:
        return await approvals.decide(
            request_id,
            approver,
            decision,
            comment,
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )


async def withdraw(platform: Platform, request_id: UUID, requester: UUID) -> ApprovalState:
    async with engine(platform) as approvals:
        return await approvals.withdraw(request_id, requester)


async def latest_state(
    platform: Platform, entity_id: UUID, *, entity_type: str = ENTITY
) -> ApprovalState | None:
    async with engine(platform) as approvals:
        return await approvals.state_of(entity_type, entity_id)


@dataclass(slots=True, frozen=True)
class Cast:
    """The people an approval route names, as employee ids.

    The engine works in employee ids — never user ids — so these are the ids of
    real `employees` rows, and the two HR members and the administrator are
    employees whose *accounts* hold the role.
    """

    department: UUID
    position: UUID
    requester: UUID
    manager: UUID
    hr: UUID
    other_hr: UUID
    outsider: UUID
    admin: UUID


@pytest.fixture
async def cast(platform: Platform) -> Cast:
    """A department, its position, a request route through it, and two HR members."""
    department = await platform.department("operaciones")
    position = await platform.position(department, "technician")

    requester = await platform.employee()
    manager = await platform.employee()
    # The per-assignment approver, which is what the route reads first.
    await platform.assign(requester, department, position, manager_employee_id=manager)

    hr = await platform.grant_account(roles=("hr",), sign_in=False)
    other_hr = await platform.grant_account(roles=("hr",), sign_in=False)
    admin = await platform.grant_account(roles=("admin",), sign_in=False)
    return Cast(
        department=UUID(department),
        position=UUID(position),
        requester=UUID(requester),
        manager=UUID(manager),
        hr=UUID(hr.employee_id),
        other_hr=UUID(other_hr.employee_id),
        outsider=UUID(await platform.employee()),
        admin=UUID(admin.employee_id),
    )


@pytest.fixture
async def app_connection(settings: Settings) -> AsyncIterator[async_sessionmaker]:
    """Sessions bound to the restricted runtime role, on the test database."""
    restricted = create_async_engine(settings.runtime_test_database_url)
    try:
        yield async_sessionmaker(bind=restricted, expire_on_commit=False)
    finally:
        await restricted.dispose()


# --- the interface is four operations and nothing else ---------------------


def test_the_public_surface_is_exactly_the_four_operations() -> None:
    """`docs/architecture/codebase-design.md` §2.3 fixes the interface.

    A fifth operation is how "approved but not yet in force" gets into the engine:
    an `apply_due_requests` or an `effective_at` argument would have to know what
    each document means, and the depth of the module goes with it. Pinned here
    because the growth is always well-intentioned and always incremental.
    """
    public = {name for name in vars(ApprovalService) if not name.startswith("_")}

    assert public == {"submit", "decide", "withdraw", "state_of"}


# --- the happy path --------------------------------------------------------


async def test_the_happy_path_runs_through_both_levels(platform: Platform, cast: Cast) -> None:
    entity = uuid4()

    request_id = await submit(platform, entity, cast.requester)
    submitted = await latest_state(platform, entity)
    assert submitted is not None
    assert submitted.id == request_id
    assert submitted.status is ApprovalStatus.PENDING_FIRST
    assert submitted.round == 1
    assert submitted.decided_at is None
    assert submitted.pending_step is not None
    assert submitted.pending_step.approver_employee_id == cast.manager

    after_manager = await decide(platform, request_id, cast.manager, DecisionKind.APPROVE, "fine")
    assert after_manager.status is ApprovalStatus.PENDING_SECOND
    assert after_manager.decided_at is None, "a request still in flight is not decided"
    assert after_manager.pending_step is not None
    # Level 2 is a role, not a person: the step deliberately names nobody.
    assert after_manager.pending_step.approver_employee_id is None

    approved = await decide(platform, request_id, cast.hr, DecisionKind.APPROVE, "recorded")
    assert approved.status is ApprovalStatus.APPROVED
    assert approved.decided_at is not None
    assert approved.pending_step is None
    assert [(d.level, d.decision) for d in approved.decisions] == [
        (1, StepStatus.APPROVED),
        (2, StepStatus.APPROVED),
    ]
    assert [d.comment for d in approved.decisions] == ["fine", "recorded"]

    assert (await latest_state(platform, entity)).status is ApprovalStatus.APPROVED


# --- rule 1: level 1 is the primary position's approver --------------------


async def test_the_primary_positions_manager_is_the_first_approver(
    platform: Platform, cast: Cast
) -> None:
    entity = uuid4()

    await submit(platform, entity, cast.requester)

    state = await latest_state(platform, entity)
    assert state.pending_step.approver_employee_id == cast.manager


async def test_the_department_manager_is_the_fallback(platform: Platform, cast: Cast) -> None:
    """A department of one still has somebody accountable for it."""
    head = await platform.employee()
    await platform.sql(
        "UPDATE employee_assignments SET manager_employee_id = NULL WHERE employee_id = :id",
        {"id": str(cast.requester)},
    )
    await platform.sql(
        "UPDATE departments SET manager_employee_id = :head WHERE id = :department",
        {"head": head, "department": str(cast.department)},
    )
    entity = uuid4()

    await submit(platform, entity, cast.requester)

    state = await latest_state(platform, entity)
    assert state.pending_step.approver_employee_id == UUID(head)


async def test_the_primary_position_decides_the_route(platform: Platform, cast: Cast) -> None:
    """A second position does not change who signs off somebody's requests."""
    elsewhere = await platform.department("finanzas")
    other_position = await platform.position(elsewhere, "analyst")
    second_manager = await platform.employee()
    await platform.assign(
        str(cast.requester),
        elsewhere,
        other_position,
        manager_employee_id=second_manager,
    )
    entity = uuid4()

    await submit(platform, entity, cast.requester)

    state = await latest_state(platform, entity)
    assert state.pending_step.approver_employee_id == cast.manager


async def test_a_requester_with_no_approver_anywhere_cannot_submit(platform: Platform) -> None:
    """Neither the position nor its department names anybody."""
    department = await platform.department("sola")
    position = await platform.position(department, "only")
    requester = await platform.employee()
    await platform.assign(requester, department, position)
    entity = uuid4()

    with pytest.raises(DomainError) as refusal:
        await submit(platform, entity, UUID(requester))

    assert refusal.value.code is ApprovalErrorCode.APPROVAL_APPROVER_UNRESOLVED
    assert refusal.value.http_status == 422
    assert await latest_state(platform, entity) is None, "a refused submission left a request"


async def test_a_submission_for_an_unknown_employee_is_refused(platform: Platform) -> None:
    with pytest.raises(DomainError) as refusal:
        await submit(platform, uuid4(), uuid4())

    assert refusal.value.code is ApprovalErrorCode.EMPLOYEE_NOT_FOUND


# --- rule 2 and 3: level 2 is HR, and nobody approves their own request ----


async def test_either_hr_member_can_decide_the_second_level(platform: Platform, cast: Cast) -> None:
    """Level 2 is a role rather than a name, so it does not wait for one person."""
    entity = uuid4()
    request_id = await submit(platform, entity, cast.requester)
    await decide(platform, request_id, cast.manager, DecisionKind.APPROVE)

    approved = await decide(platform, request_id, cast.other_hr, DecisionKind.APPROVE, "ok")

    assert approved.status is ApprovalStatus.APPROVED


async def test_self_approval_skips_the_first_level(platform: Platform) -> None:
    """A department head filing their own request is their own approver.

    The level is recorded as passed over, with the reason, and the request goes
    straight to HR. Silently reassigning it to somebody else would invent an
    approver the route never named.
    """
    department = await platform.department("direccion")
    position = await platform.position(department, "head")
    head = UUID(await platform.employee())
    await platform.assign(str(head), department, position)
    await platform.sql(
        "UPDATE departments SET manager_employee_id = :head WHERE id = :department",
        {"head": str(head), "department": department},
    )
    hr = await platform.grant_account(roles=("hr",), sign_in=False)
    entity = uuid4()

    request_id = await submit(platform, entity, head)

    state = await latest_state(platform, entity)
    assert state.status is ApprovalStatus.PENDING_SECOND
    assert state.round == 1
    skipped = state.steps[0]
    assert (skipped.level, skipped.round, skipped.status) == (1, 1, StepStatus.SKIPPED)
    assert skipped.approver_employee_id == head
    assert state.decisions[0].decision is StepStatus.SKIPPED
    assert state.decisions[0].comment == SELF_APPROVAL_REASON
    assert state.pending_step.level == 2
    # And it can still be finished by somebody else.
    approved = await decide(platform, request_id, UUID(hr.employee_id), DecisionKind.APPROVE)
    assert approved.status is ApprovalStatus.APPROVED


async def test_an_hr_requester_cannot_decide_their_own_second_level(platform: Platform) -> None:
    """Holding `hr` does not make somebody their own second approver."""
    department = await platform.department("rrhh")
    position = await platform.position(department, "generalist")
    hr_requester = await platform.grant_account(roles=("hr",), sign_in=False)
    manager = await platform.employee()
    await platform.assign(
        hr_requester.employee_id, department, position, manager_employee_id=manager
    )
    colleague = await platform.grant_account(roles=("hr",), sign_in=False)
    entity = uuid4()

    request_id = await submit(platform, entity, UUID(hr_requester.employee_id))
    await decide(platform, request_id, UUID(manager), DecisionKind.APPROVE)

    with pytest.raises(DomainError) as refusal:
        await decide(platform, request_id, UUID(hr_requester.employee_id), DecisionKind.APPROVE)
    assert refusal.value.code is ApprovalErrorCode.APPROVAL_NOT_APPROVER
    assert refusal.value.http_status == 403

    approved = await decide(platform, request_id, UUID(colleague.employee_id), DecisionKind.APPROVE)
    assert approved.status is ApprovalStatus.APPROVED
    assert approved.decisions[-1].approver_employee_id == UUID(colleague.employee_id)


async def test_a_request_with_no_hr_other_than_the_requester_is_refused(
    platform: Platform,
) -> None:
    """Refused at submission rather than discovered, stuck, at the second level."""
    department = await platform.department("rrhh")
    position = await platform.position(department, "only")
    requester = await platform.grant_account(roles=("hr",), sign_in=False)
    manager = await platform.employee()
    await platform.assign(requester.employee_id, department, position, manager_employee_id=manager)

    with pytest.raises(DomainError) as refusal:
        await submit(platform, uuid4(), UUID(requester.employee_id))

    assert refusal.value.code is ApprovalErrorCode.APPROVAL_HR_UNAVAILABLE


# --- rule 5: who may decide ------------------------------------------------


@pytest.mark.parametrize("actor", ["outsider", "requester", "admin"])
async def test_nobody_else_can_decide_level_one(
    platform: Platform, cast: Cast, actor: str
) -> None:
    """An administrator included: the engine decides on the route, not on roles."""
    entity = uuid4()
    request_id = await submit(platform, entity, cast.requester)

    with pytest.raises(DomainError) as refusal:
        await decide(platform, request_id, getattr(cast, actor), DecisionKind.APPROVE)

    assert refusal.value.code is ApprovalErrorCode.APPROVAL_NOT_APPROVER
    assert refusal.value.http_status == 403
    assert (await latest_state(platform, entity)).status is ApprovalStatus.PENDING_FIRST


async def test_a_non_hr_member_cannot_decide_level_two(platform: Platform, cast: Cast) -> None:
    entity = uuid4()
    request_id = await submit(platform, entity, cast.requester)
    await decide(platform, request_id, cast.manager, DecisionKind.APPROVE)

    with pytest.raises(DomainError) as refusal:
        await decide(platform, request_id, cast.outsider, DecisionKind.APPROVE)

    assert refusal.value.code is ApprovalErrorCode.APPROVAL_NOT_APPROVER


async def test_a_decision_on_an_unknown_request_is_refused(platform: Platform) -> None:
    with pytest.raises(DomainError) as refusal:
        await decide(platform, uuid4(), uuid4(), DecisionKind.APPROVE)

    assert refusal.value.code is ApprovalErrorCode.APPROVAL_NOT_FOUND


async def test_a_decision_on_a_closed_request_is_refused(platform: Platform, cast: Cast) -> None:
    entity = uuid4()
    request_id = await submit(platform, entity, cast.requester)
    await withdraw(platform, request_id, cast.requester)

    with pytest.raises(DomainError) as refusal:
        await decide(platform, request_id, cast.manager, DecisionKind.APPROVE)

    assert refusal.value.code is ApprovalErrorCode.APPROVAL_NOT_PENDING


# --- reject ----------------------------------------------------------------


async def test_a_rejection_at_the_first_level_is_final(platform: Platform, cast: Cast) -> None:
    entity = uuid4()
    request_id = await submit(platform, entity, cast.requester)

    rejected = await decide(platform, request_id, cast.manager, DecisionKind.REJECT, "no budget")

    assert rejected.status is ApprovalStatus.REJECTED
    assert rejected.decided_at is not None
    assert rejected.decisions[-1].comment == "no budget"
    with pytest.raises(DomainError) as refusal:
        await submit(platform, entity, cast.requester)
    assert refusal.value.code is ApprovalErrorCode.APPROVAL_PREVIOUSLY_REJECTED
    assert refusal.value.http_status == 409


async def test_a_rejection_at_the_second_level_is_final(platform: Platform, cast: Cast) -> None:
    entity = uuid4()
    request_id = await submit(platform, entity, cast.requester)
    await decide(platform, request_id, cast.manager, DecisionKind.APPROVE)

    rejected = await decide(platform, request_id, cast.hr, DecisionKind.REJECT, "not this month")

    assert rejected.status is ApprovalStatus.REJECTED
    assert rejected.decided_at is not None
    with pytest.raises(DomainError) as refusal:
        await submit(platform, entity, cast.requester)
    assert refusal.value.code is ApprovalErrorCode.APPROVAL_PREVIOUSLY_REJECTED


# --- return for correction, and the round it opens -------------------------


async def test_a_return_at_the_first_level_reopens_the_round(
    platform: Platform, cast: Cast
) -> None:
    entity = uuid4()
    request_id = await submit(platform, entity, cast.requester)

    returned = await decide(
        platform, request_id, cast.manager, DecisionKind.RETURN, "fix the dates"
    )

    assert returned.status is ApprovalStatus.DRAFT
    assert returned.decided_at is None, "a returned request is not decided, it is back"
    assert returned.pending_step is None
    # The decision stays readable after the request has gone back.
    assert [(d.level, d.decision) for d in returned.decisions] == [(1, StepStatus.RETURNED)]
    assert returned.decisions[0].comment == "fix the dates"

    again = await submit(platform, entity, cast.requester)
    assert again == request_id, "a returned request is resumed, not replaced"

    second_round = await latest_state(platform, entity)
    assert second_round.round == 2
    assert second_round.status is ApprovalStatus.PENDING_FIRST
    assert {(s.round, s.level, s.status) for s in second_round.steps} == {
        (1, 1, StepStatus.RETURNED),
        (2, 1, StepStatus.PENDING),
    }
    assert [(d.round, d.level) for d in second_round.decisions] == [(1, 1)]
    assert second_round.pending_step.approver_employee_id == cast.manager

    await decide(platform, again, cast.manager, DecisionKind.APPROVE)
    approved = await decide(platform, again, cast.hr, DecisionKind.APPROVE)
    assert approved.status is ApprovalStatus.APPROVED
    assert [(d.round, d.level, d.decision) for d in approved.decisions] == [
        (1, 1, StepStatus.RETURNED),
        (2, 1, StepStatus.APPROVED),
        (2, 2, StepStatus.APPROVED),
    ], "the history of the returned attempt must survive the round that replaced it"


async def test_a_return_at_the_second_level_reopens_the_round(
    platform: Platform, cast: Cast
) -> None:
    entity = uuid4()
    request_id = await submit(platform, entity, cast.requester)
    await decide(platform, request_id, cast.manager, DecisionKind.APPROVE)

    returned = await decide(
        platform, request_id, cast.hr, DecisionKind.RETURN, "attach the contract"
    )

    assert returned.status is ApprovalStatus.DRAFT
    assert [(d.level, d.decision) for d in returned.decisions] == [
        (1, StepStatus.APPROVED),
        (2, StepStatus.RETURNED),
    ]

    await submit(platform, entity, cast.requester)

    second_round = await latest_state(platform, entity)
    assert second_round.round == 2
    assert {(s.round, s.level, s.status) for s in second_round.steps} == {
        (1, 1, StepStatus.APPROVED),
        (1, 2, StepStatus.RETURNED),
        (2, 1, StepStatus.PENDING),
    }
    assert len(second_round.decisions) == 2, "the first round's decisions are still there"


# --- withdraw --------------------------------------------------------------


async def test_the_requester_can_withdraw_while_pending_first(
    platform: Platform, cast: Cast
) -> None:
    entity = uuid4()
    request_id = await submit(platform, entity, cast.requester)

    withdrawn = await withdraw(platform, request_id, cast.requester)

    assert withdrawn.status is ApprovalStatus.WITHDRAWN
    assert withdrawn.decided_at is not None
    assert (await latest_state(platform, entity)).status is ApprovalStatus.WITHDRAWN


async def test_the_requester_can_withdraw_a_returned_draft(
    platform: Platform, cast: Cast
) -> None:
    """A returned request sits in draft, and draft is the requester's own."""
    entity = uuid4()
    request_id = await submit(platform, entity, cast.requester)
    returned = await decide(
        platform, request_id, cast.manager, DecisionKind.RETURN, "not like this"
    )
    assert returned.status is ApprovalStatus.DRAFT, "a return is what puts a request back in draft"

    withdrawn = await withdraw(platform, request_id, cast.requester)

    assert withdrawn.status is ApprovalStatus.WITHDRAWN


async def test_withdrawing_once_hr_has_it_is_refused(platform: Platform, cast: Cast) -> None:
    entity = uuid4()
    request_id = await submit(platform, entity, cast.requester)
    await decide(platform, request_id, cast.manager, DecisionKind.APPROVE)

    with pytest.raises(DomainError) as refusal:
        await withdraw(platform, request_id, cast.requester)

    assert refusal.value.code is ApprovalErrorCode.APPROVAL_NOT_WITHDRAWABLE
    assert refusal.value.http_status == 409
    assert (await latest_state(platform, entity)).status is ApprovalStatus.PENDING_SECOND


async def test_only_the_requester_can_withdraw(platform: Platform, cast: Cast) -> None:
    entity = uuid4()
    request_id = await submit(platform, entity, cast.requester)

    with pytest.raises(DomainError) as refusal:
        await withdraw(platform, request_id, cast.manager)

    assert refusal.value.code is ApprovalErrorCode.APPROVAL_NOT_REQUESTER
    assert refusal.value.http_status == 403


async def test_withdrawing_an_unknown_request_is_refused(platform: Platform) -> None:
    with pytest.raises(DomainError) as refusal:
        await withdraw(platform, uuid4(), uuid4())

    assert refusal.value.code is ApprovalErrorCode.APPROVAL_NOT_FOUND


async def test_a_withdrawn_entity_can_be_submitted_again(platform: Platform, cast: Cast) -> None:
    """Withdrawal is the requester taking their own request back, not a verdict.

    Only a rejection is final for the entity; the abandoned request keeps its own
    history either way.
    """
    entity = uuid4()
    first = await submit(platform, entity, cast.requester)
    await withdraw(platform, first, cast.requester)

    second = await submit(platform, entity, cast.requester)

    assert second != first
    latest = await latest_state(platform, entity)
    assert latest.id == second
    assert latest.status is ApprovalStatus.PENDING_FIRST
    assert latest.round == 1, "a fresh request is a first attempt, not a round of the old one"
    assert (
        await platform.scalar(
            "SELECT status FROM approval_requests WHERE id = :id", {"id": first}
        )
        == "withdrawn"
    )


# --- one open request per entity, in the service and in the database -------


async def test_a_second_submission_while_one_is_open_is_refused(
    platform: Platform, cast: Cast
) -> None:
    entity = uuid4()
    await submit(platform, entity, cast.requester)

    with pytest.raises(DomainError) as refusal:
        await submit(platform, entity, cast.requester)

    assert refusal.value.code is ApprovalErrorCode.APPROVAL_ALREADY_OPEN
    assert refusal.value.http_status == 409


async def test_the_database_refuses_a_second_open_request(
    platform: Platform, cast: Cast, app_connection: async_sessionmaker
) -> None:
    """The service's check is what a race runs around; this is the real rule.

    Written over the restricted role, so it holds for the connection requests
    actually use — and so the guarantee does not depend on the service having
    remembered to ask.
    """
    entity = uuid4()
    await submit(platform, entity, cast.requester)
    insert = text(
        """
        INSERT INTO approval_requests (id, entity_type, entity_id, requester_employee_id,
                                       status, round)
        VALUES (:id, :entity_type, :entity_id, :requester, :status, 1)
        """
    )
    values = {
        "id": uuid4(),
        "entity_type": ENTITY,
        "entity_id": entity,
        "requester": cast.requester,
    }

    async with app_connection() as session:
        with pytest.raises(Exception) as excinfo:
            await session.execute(insert, {**values, "status": "pending_first"})
    assert "uq_approval_requests_open" in str(excinfo.value)

    # Closed statuses are outside the index, which is what makes it partial: the
    # same entity may be filed again once nothing is open.
    async with app_connection() as session:
        await session.execute(insert, {**values, "status": "withdrawn"})
        await session.commit()


async def test_the_open_request_index_covers_exactly_the_open_statuses(
    platform: Platform,
) -> None:
    """The predicate is written in SQL and the statuses in Python.

    A status added to one and not the other would let two requests be open at
    once, or refuse one that should be allowed, without any code looking wrong.
    """
    definition = await platform.scalar(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_approval_requests_open'"
    )

    assert "UNIQUE" in definition
    for status in OPEN_STATUSES:
        assert f"'{status.value}'" in definition
    for status in ApprovalStatus:
        if status not in OPEN_STATUSES:
            assert f"'{status.value}'" not in definition


# --- rule 6: the engine knows nothing about what it approves ---------------


async def test_the_engine_approves_an_entity_type_it_has_never_heard_of(
    platform: Platform, cast: Cast
) -> None:
    """No table, no model, no code path anywhere knows this entity type.

    This is the ticket's own requirement, and the test that fails the day somebody
    teaches the engine what a personnel change is: the moment approving something
    depends on reading it, an entity type nobody has written a reader for stops
    working, and this stops reaching `approved`.
    """
    entity = uuid4()
    unknown = "a.document.that.does.not.exist"

    request_id = await submit(platform, entity, cast.requester, entity_type=unknown)
    await decide(platform, request_id, cast.manager, DecisionKind.APPROVE, "looks fine")
    approved = await decide(platform, request_id, cast.hr, DecisionKind.APPROVE, "approved")

    assert approved.status is ApprovalStatus.APPROVED
    assert approved.entity_type == unknown
    assert approved.entity_id == entity
    assert (await latest_state(platform, entity, entity_type=unknown)).status is (
        ApprovalStatus.APPROVED
    )
    assert await latest_state(platform, entity) is None, "the same id under another type"


async def test_state_of_an_entity_that_never_had_a_request_is_nothing(
    platform: Platform,
) -> None:
    assert await latest_state(platform, uuid4()) is None


# --- the submission context ------------------------------------------------


async def test_the_submission_context_is_stored(platform: Platform, cast: Cast) -> None:
    """DESIGN §3.4 keeps `initiated_by` on the request: that is where an
    agent-proposed document is told apart from one a person filed."""
    entity = uuid4()
    confirmer = uuid4()

    await submit(
        platform,
        entity,
        cast.requester,
        context=SubmitContext(initiated_by="agent", confirmed_by_user_id=confirmer),
    )

    row = await platform.sql(
        "SELECT initiated_by, confirmed_by_user_id FROM approval_requests "
        "WHERE entity_id = :id",
        {"id": entity},
    )
    assert row[0][0] == "agent"
    assert row[0][1] == confirmer


async def test_an_unknown_initiator_is_refused(platform: Platform, cast: Cast) -> None:
    with pytest.raises(DomainError) as refusal:
        await submit(
            platform,
            uuid4(),
            cast.requester,
            context=SubmitContext(initiated_by="robot"),
        )

    assert refusal.value.code is ApprovalErrorCode.INVALID_REQUEST


# --- rule 8: every decision is audited -------------------------------------


async def test_every_decision_is_recorded_in_the_audit_log(
    platform: Platform, cast: Cast
) -> None:
    """Approver, decision, comment and level, with the actor the caller knew."""
    entity = uuid4()
    request_id = await submit(platform, entity, cast.requester)
    actor = uuid4()

    await decide(
        platform,
        request_id,
        cast.manager,
        DecisionKind.APPROVE,
        "looks fine",
        actor_user_id=actor,
        actor_roles=frozenset({"manager", "employee"}),
    )
    await decide(platform, request_id, cast.hr, DecisionKind.REJECT, "not this month")

    rows = await platform.sql(
        "SELECT actor_user_id, entity_type, entity_id, after FROM audit_log "
        "WHERE action = 'approval.decided' ORDER BY id"
    )

    assert len(rows) == 2
    first, second = rows
    assert first[0] == actor
    assert (first[1], first[2]) == (ENTITY, entity)
    assert first[3]["level"] == 1
    assert first[3]["decision"] == "approved"
    assert first[3]["approver_employee_id"] == str(cast.manager)
    assert first[3]["comment"] == "looks fine"
    assert first[3]["approval_request_id"] == str(request_id)
    assert second[3]["level"] == 2
    assert second[3]["decision"] == "rejected"
    assert second[3]["approver_employee_id"] == str(cast.hr)


async def test_a_skipped_level_is_recorded_in_the_audit_log(platform: Platform) -> None:
    """A level passed over is a decision the trail has to be able to explain."""
    department = await platform.department("direccion")
    position = await platform.position(department, "head")
    head = UUID(await platform.employee())
    await platform.assign(str(head), department, position)
    await platform.sql(
        "UPDATE departments SET manager_employee_id = :head WHERE id = :department",
        {"head": str(head), "department": department},
    )
    await platform.grant_account(roles=("hr",), sign_in=False)

    await submit(platform, uuid4(), head)

    row = await platform.sql("SELECT after FROM audit_log WHERE action = 'approval.decided'")
    assert len(row) == 1
    assert row[0][0]["decision"] == "skipped"
    assert row[0][0]["level"] == 1
    assert row[0][0]["comment"] == SELF_APPROVAL_REASON


async def test_filing_a_request_is_recorded(platform: Platform, cast: Cast) -> None:
    """Filing is an act somebody performed, and the route it took is part of it.

    Without this the request row is the only trace, and it says nothing about who
    filed it — which is the first question an incident review asks.
    """
    request_id = await submit(platform, uuid4(), cast.requester)

    row = await platform.sql("SELECT after FROM audit_log WHERE action = 'approval.submitted'")
    assert len(row) == 1
    assert row[0][0]["approval_request_id"] == str(request_id)
    assert row[0][0]["requester_employee_id"] == str(cast.requester)
    assert row[0][0]["level_one_approver_employee_id"] == str(cast.manager)
    assert row[0][0]["round"] == 1


async def test_withdrawing_a_request_is_recorded(platform: Platform, cast: Cast) -> None:
    """A withdrawal is a state change; the requester's own id is not enough."""
    request_id = await submit(platform, uuid4(), cast.requester)

    async with engine(platform) as approvals:
        await approvals.withdraw(request_id, cast.requester)

    row = await platform.sql("SELECT after FROM audit_log WHERE action = 'approval.withdrawn'")
    assert len(row) == 1
    assert row[0][0]["approval_request_id"] == str(request_id)
    assert row[0][0]["from_status"] == "pending_first"


# --- the decision record is append-only ------------------------------------

@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE approval_decisions SET decision = 'approved'",
        "DELETE FROM approval_decisions",
    ],
)
async def test_the_decision_record_cannot_be_rewritten(
    platform: Platform, cast: Cast, app_connection: async_sessionmaker, statement: str
) -> None:
    """Refused by PostgreSQL, not by a code path somebody has to remember.

    Driven over the restricted role because that is the connection requests use;
    the owner can still rewrite the table, which is exactly why the two
    connections are configured separately.
    """
    entity = uuid4()
    request_id = await submit(platform, entity, cast.requester)
    await decide(platform, request_id, cast.manager, DecisionKind.APPROVE)

    async with app_connection() as session:
        with pytest.raises(Exception) as excinfo:
            await session.execute(text(statement))

    assert "permission denied" in str(excinfo.value).lower()


async def test_the_decision_record_can_still_be_appended_to_and_read(
    platform: Platform, cast: Cast, app_connection: async_sessionmaker
) -> None:
    """Append-only, not read-only: the engine has to be able to record a decision."""
    entity = uuid4()
    request_id = await submit(platform, entity, cast.requester)
    await decide(platform, request_id, cast.manager, DecisionKind.APPROVE)

    async with app_connection() as session:
        await session.execute(
            text(
                """
                INSERT INTO approval_decisions (id, request_id, level, round,
                                                approver_employee_id, decision, comment,
                                                decided_at)
                VALUES (:id, :request_id, 1, 99, :approver, 'approved', 'probe', now())
                """
            ),
            {"id": uuid4(), "request_id": request_id, "approver": cast.manager},
        )
        await session.commit()
        written = await session.scalar(
            text("SELECT count(*) FROM approval_decisions WHERE comment = 'probe'")
        )

    assert written == 1
    assert await platform.scalar(
        "SELECT count(*) FROM approval_decisions WHERE request_id = :id", {"id": request_id}
    ) == 2
