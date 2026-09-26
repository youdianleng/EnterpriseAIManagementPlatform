"""The notification centre, and the events that fill it.

Everything here runs against a real PostgreSQL through real sessions: the claims
are about queries and constraints — a unique index that refuses the second write, a
list that excludes expired rows while the rows stay, one statement that answers
"not yours" and "no such row" identically — and a substitute would answer them with
the test's own assumptions.

Three groups, and the third is the one that matters most:

1. **The caller's own centre.** Newest first, paginated, unread-only, expired rows
   filtered from the list and kept in the table, an unread count that agrees with
   the list, and marking read one row at a time or all of them.
2. **Nobody else's.** A 403 that is byte-for-byte the same whether the notification
   belongs to somebody else or does not exist — because an endpoint that answers
   that question differently is an existence oracle.
3. **The approval events.** Driven through the real engine and the real notifier
   (`domain/notification/approval.py`), which is what proves the wiring the ticket
   asks for: submitted, approved at either level, rejected, returned, withdrawn.
   The engine is not modified by this ticket, so the test constructs the pair the
   way a caller does.

Idempotency is asserted twice over: at the event level (the same raise twice) and
at the record level (the counts of notifications and of delivery rows).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.domain.approval.models import DecisionKind
from app.domain.approval.service import ApprovalService
from app.domain.notification.approval import ApprovalNotifier
from app.domain.notification.models import (
    NOT_ATTEMPTED,
    NotificationDraft,
    NotificationType,
    RaiseOutcome,
)
from app.domain.notification.service import NotificationService
from app.repositories.approval import PostgresApprovalRepository
from app.repositories.notification import PostgresNotificationRepository
from tests.support.platform import Actor, Platform

PATH = "/api/v1/notifications"

#: An entity type with no table behind it: the notifier, like the approval engine,
#: carries a pair of identifiers and never interprets them.
ENTITY = "leave_request"

AWAITING = NotificationType.APPROVAL_AWAITING_DECISION.value


def uid(actor: Actor) -> UUID:
    """The employee id as the domain takes it.

    The API talks in strings and the domain in UUIDs, and mixing the two silently is
    how a test ends up asserting against a row nobody wrote.
    """
    return UUID(actor.employee_id)


# --- the service, on its own session, the way a request uses it --------------


@asynccontextmanager
async def service(platform: Platform) -> AsyncIterator[NotificationService]:
    async with platform.factory() as session:
        yield NotificationService(PostgresNotificationRepository(session), session)


@asynccontextmanager
async def approval_flow(platform: Platform) -> AsyncIterator[ApprovalNotifier]:
    """The engine and its notifier, wired the way a caller wires them.

    One session for both, like one request: the engine commits its decision, then
    the notifier writes what that decision owes.
    """
    async with platform.factory() as session:
        repository = PostgresApprovalRepository(session)
        engine = ApprovalService(repository, session)
        notifications = NotificationService(PostgresNotificationRepository(session), session)
        yield ApprovalNotifier(engine, notifications, repository)


async def raise_notification(
    platform: Platform,
    recipient: UUID,
    *,
    entity_id: UUID | None = None,
    event: str = "r1:l1",
    notification_type: NotificationType = NotificationType.APPROVAL_AWAITING_DECISION,
    expires_at: datetime | None = None,
) -> RaiseOutcome:
    draft = NotificationDraft(
        recipient_employee_id=recipient,
        type=notification_type,
        payload={"approval_request_id": str(uuid4()), "round": 1, "level": 1},
        entity_type=ENTITY,
        entity_id=entity_id or uuid4(),
        event=event,
        expires_at=expires_at,
    )
    async with service(platform) as notifications:
        return await notifications.notify(draft)


async def inbox(actor: Actor, **params: object) -> dict:
    """One page of the caller's own notifications, as the screen asks for it."""
    query = urlencode({key: value for key, value in params.items() if value is not None})
    response = await actor.get(f"{PATH}?{query}" if query else PATH)
    assert response.status_code == 200, response.text
    return response.json()


async def unread(actor: Actor) -> int:
    response = await actor.get(f"{PATH}/unread-count")
    assert response.status_code == 200, response.text
    return response.json()["unread"]


async def count(platform: Platform, table: str, where: str = "true") -> int:
    return int(await platform.scalar(f"SELECT count(*) FROM {table} WHERE {where}") or 0)


# --- the caller's own centre --------------------------------------------------


async def test_the_list_is_the_callers_own_newest_first(platform: Platform) -> None:
    mine = await platform.account()
    someone_else = await platform.account()
    raised = [
        await raise_notification(platform, uid(mine), event=event)
        for event in ("r1:l1", "r1:l2", "r2:l1")
    ]
    await raise_notification(platform, uid(someone_else), event="r1:l1")

    page = await inbox(mine)

    assert page["total"] == 3
    assert page["limit"] == 50
    # Newest first: the three ids come back in the opposite order to the writes.
    assert [item["id"] for item in page["items"]] == [
        str(outcome.notification_id) for outcome in reversed(raised)
    ]
    assert all(item["read_at"] is None for item in page["items"])
    # Neither the dedupe key nor the recipient is published: the first is the
    # notifier's business, the second is the caller on every row.
    assert "dedupe_key" not in page["items"][0]
    assert "recipient_employee_id" not in page["items"][0]
    # Somebody else's notification is not in this caller's page at all.
    assert (await inbox(someone_else))["total"] == 1


async def test_unread_only_shows_what_is_still_unread(platform: Platform) -> None:
    mine = await platform.account()
    first = await raise_notification(platform, uid(mine), event="r1:l1")
    await raise_notification(platform, uid(mine), event="r2:l1")

    await mine.post(f"{PATH}/{first.notification_id}/read")

    everything = await inbox(mine)
    unread_only = await inbox(mine, unread_only="true")

    assert everything["total"] == 2
    assert unread_only["total"] == 1
    assert unread_only["items"][0]["read_at"] is None


async def test_paging_reports_the_total_it_is_a_page_of(platform: Platform) -> None:
    mine = await platform.account()
    for event in ("r1:l1", "r2:l1", "r3:l1"):
        await raise_notification(platform, uid(mine), event=event)

    first = await inbox(mine, limit=2, offset=0)
    second = await inbox(mine, limit=2, offset=2)

    assert first["total"] == second["total"] == 3
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert first["items"][0]["id"] != second["items"][0]["id"]


async def test_an_expired_notification_leaves_the_list_and_stays_in_the_table(
    platform: Platform,
) -> None:
    """Both halves of the requirement, and the second is the one that matters.

    "No longer in the list" is a read filter; "the record is kept" is what lets
    somebody answer whether the person was told, months later.
    """
    mine = await platform.account()
    await raise_notification(platform, uid(mine), event="r1:l1")
    expired = await raise_notification(
        platform,
        uid(mine),
        event="r2:l1",
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )

    page = await inbox(mine)

    assert page["total"] == 1
    assert expired.notification_id not in [item["id"] for item in page["items"]]
    # Kept: the row and its delivery record are still there, and still readable.
    assert await count(platform, "notifications") == 2
    assert (
        await count(
            platform, "notification_deliveries", f"notification_id = '{expired.notification_id}'"
        )
        == 2
    )


async def test_the_unread_count_ignores_expired_rows(platform: Platform) -> None:
    """The badge counts the rows the list would show, or it sends people nowhere."""
    mine = await platform.account()
    await raise_notification(
        platform,
        uid(mine),
        event="r1:l1",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    await raise_notification(platform, uid(mine), event="r2:l1")

    assert await unread(mine) == 1
    assert (await inbox(mine, unread_only="true"))["total"] == 1


async def test_the_page_size_is_bounded(platform: Platform) -> None:
    mine = await platform.account()

    response = await mine.get(f"{PATH}?limit=100000")

    assert response.status_code == 422


async def test_a_negative_offset_is_refused(platform: Platform) -> None:
    mine = await platform.account()

    response = await mine.get(f"{PATH}?offset=-1")

    assert response.status_code == 422


@pytest.mark.parametrize(
    "roles",
    [("admin",), ("hr",), ("finance",), ("it",), ("compliance",), ("manager",), ("employee",)],
)
async def test_every_role_gets_its_own_centre(
    platform: Platform, roles: tuple[str, ...]
) -> None:
    """The centre is self-service, so no role is refused it — including the ones
    whose job is somewhere else entirely."""
    actor = await platform.account(roles=roles)
    await raise_notification(platform, uid(actor))

    assert (await inbox(actor))["total"] == 1
    assert await unread(actor) == 1


# --- marking read -------------------------------------------------------------


async def test_marking_one_read_stamps_it_and_clears_the_badge(platform: Platform) -> None:
    mine = await platform.account()
    raised = await raise_notification(platform, uid(mine), event="r1:l1")

    response = await mine.post(f"{PATH}/{raised.notification_id}/read")

    assert response.status_code == 200, response.text
    marked = response.json()
    assert marked["id"] == str(raised.notification_id)
    assert marked["read_at"] is not None
    assert await unread(mine) == 0


async def test_marking_the_same_one_twice_keeps_the_first_timestamp(platform: Platform) -> None:
    """A second click is not a mistake, and "when did they see it" has one answer."""
    mine = await platform.account()
    raised = await raise_notification(platform, uid(mine))

    first = await mine.post(f"{PATH}/{raised.notification_id}/read")
    second = await mine.post(f"{PATH}/{raised.notification_id}/read")

    assert second.status_code == 200, second.text
    assert second.json()["read_at"] == first.json()["read_at"]


async def test_marking_all_read_clears_the_badge_and_says_how_many(platform: Platform) -> None:
    mine = await platform.account()
    for event in ("r1:l1", "r2:l1", "r3:l1"):
        await raise_notification(platform, uid(mine), event=event)

    response = await mine.post(f"{PATH}/read-all")

    assert response.status_code == 200, response.text
    assert response.json()["marked"] == 3
    assert await unread(mine) == 0
    assert (await inbox(mine, unread_only="true"))["total"] == 0


async def test_marking_all_read_twice_marks_nothing_the_second_time(
    platform: Platform,
) -> None:
    mine = await platform.account()
    await raise_notification(platform, uid(mine))

    await mine.post(f"{PATH}/read-all")
    second = await mine.post(f"{PATH}/read-all")

    assert second.json()["marked"] == 0


async def test_marking_all_read_leaves_other_peoples_notifications_alone(
    platform: Platform,
) -> None:
    mine = await platform.account()
    someone_else = await platform.account()
    await raise_notification(platform, uid(mine))
    theirs = await raise_notification(platform, uid(someone_else))

    await mine.post(f"{PATH}/read-all")

    assert await unread(someone_else) == 1
    assert (
        await platform.scalar(
            "SELECT read_at FROM notifications WHERE id = :id",
            {"id": theirs.notification_id},
        )
        is None
    )


# --- nobody else's ------------------------------------------------------------


async def test_another_persons_notification_is_refused(platform: Platform) -> None:
    mine = await platform.account()
    someone_else = await platform.account()
    theirs = await raise_notification(platform, uid(someone_else))

    response = await mine.post(f"{PATH}/{theirs.notification_id}/read")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR_NTF_001"
    # And it is still unread: the refusal did not half-happen.
    assert await unread(someone_else) == 1


async def test_the_refusal_does_not_reveal_whether_the_id_exists(
    platform: Platform,
) -> None:
    """The whole point of one code for both cases.

    A 404 for an unknown id and a 403 for somebody else's would let any signed-in
    person enumerate the notification table by trying ids and reading the status.
    """
    mine = await platform.account()
    someone_else = await platform.account()
    theirs = await raise_notification(platform, uid(someone_else))

    existing = await mine.post(f"{PATH}/{theirs.notification_id}/read")
    absent = await mine.post(f"{PATH}/{uuid4()}/read")

    assert existing.status_code == absent.status_code == 403
    for field in ("code", "message_key", "message", "detail"):
        assert existing.json()["error"][field] == absent.json()["error"][field], field


async def test_a_refused_read_is_recorded(platform: Platform) -> None:
    """Who tried is the half of the trail a 403 cannot carry."""
    mine = await platform.account()
    someone_else = await platform.account()
    theirs = await raise_notification(platform, uid(someone_else))

    await mine.post(f"{PATH}/{theirs.notification_id}/read")

    rows = await platform.sql(
        "SELECT actor_user_id, entity_id, after FROM audit_log "
        "WHERE action = 'access.refused' AND entity_type = 'notification'"
    )
    assert len(rows) == 1
    assert str(rows[0][0]) == mine.user_id
    assert str(rows[0][1]) == str(theirs.notification_id)
    assert rows[0][2]["reason"] == "not_the_recipient"


async def test_an_unauthenticated_caller_reaches_nothing(platform: Platform) -> None:
    """Every endpoint, including the two that only mark things read."""
    calls = [
        ("GET", PATH),
        ("GET", f"{PATH}/unread-count"),
        ("POST", f"{PATH}/read-all"),
        ("POST", f"{PATH}/{uuid4()}/read"),
    ]

    for method, path in calls:
        response = await platform.client.request(method, path)
        assert response.status_code == 401, f"{method} {path}: {response.status_code}"


# --- idempotency --------------------------------------------------------------


async def test_the_same_event_twice_is_one_notification(platform: Platform) -> None:
    """The dedupe key, asserted where it lives: one row, one delivery per channel.

    The second raise is not an error and not a second notification. It is a
    *duplicate*, reported as such.
    """
    mine = await platform.account()
    entity = uuid4()

    first = await raise_notification(platform, uid(mine), entity_id=entity, event="r1:l1")
    second = await raise_notification(platform, uid(mine), entity_id=entity, event="r1:l1")

    assert first.created and not first.duplicate
    assert second.duplicate and not second.created
    assert await count(platform, "notifications") == 1
    # One delivery row per channel — in-app and email — and no more.
    channels = await platform.sql(
        "SELECT channel, count(*) FROM notification_deliveries GROUP BY channel ORDER BY channel"
    )
    assert [(row[0], row[1]) for row in channels] == [("email", 1), ("inapp", 1)]


async def test_a_suppressed_duplicate_is_recorded(platform: Platform) -> None:
    """Not dropped silently: the second attempt leaves a record of itself.

    Without it, "the notifier ran twice" and "the notifier never ran" are
    indistinguishable from the notification that is simply not there.
    """
    mine = await platform.account()
    entity = uuid4()

    await raise_notification(platform, uid(mine), entity_id=entity, event="r1:l1")
    await raise_notification(platform, uid(mine), entity_id=entity, event="r1:l1")

    rows = await platform.sql(
        "SELECT after FROM audit_log WHERE action = 'notification.duplicate_suppressed'"
    )
    assert len(rows) == 1
    recorded = rows[0][0]
    assert recorded["recipient_employee_id"] == mine.employee_id
    assert recorded["dedupe_key"] == f"{AWAITING}:{ENTITY}:{entity}:r1:l1"


async def test_a_different_event_is_a_second_notification(platform: Platform) -> None:
    """One key per event, not per entity: the next round is news again."""
    mine = await platform.account()
    entity = uuid4()

    await raise_notification(platform, uid(mine), entity_id=entity, event="r1:l1")
    await raise_notification(platform, uid(mine), entity_id=entity, event="r2:l1")

    assert await count(platform, "notifications") == 1 + 1


async def test_the_same_event_for_two_people_is_two_notifications(platform: Platform) -> None:
    """The key is unique *per recipient*: an approver and a requester both get told."""
    first = await platform.account()
    second = await platform.account()
    entity = uuid4()

    await raise_notification(platform, uid(first), entity_id=entity, event="r1:l1")
    await raise_notification(platform, uid(second), entity_id=entity, event="r1:l1")

    assert await count(platform, "notifications") == 2


async def test_a_delivery_row_exists_for_each_channel(platform: Platform) -> None:
    """In-app is delivered by existing; email is queued, and says why.

    Recording the email as `sent` would be a lie the first person to ask "did it go
    out" would believe — and leaving the reason empty would read as an attempt that
    failed silently, which is the other half of the same question.
    """
    mine = await platform.account()
    await raise_notification(platform, uid(mine))

    rows = await platform.sql(
        "SELECT channel, status, attempts, sent_at, error FROM notification_deliveries "
        "ORDER BY channel"
    )

    email, inapp = rows
    assert email[0] == "email"
    assert email[1] == "pending"
    assert email[2] == 0
    assert email[3] is None
    assert email[4] == NOT_ATTEMPTED
    assert inapp[0] == "inapp"
    assert inapp[1] == "sent"
    assert inapp[2] == 1
    assert inapp[3] is not None
    assert inapp[4] is None


# --- the record's shape, refused by the database ------------------------------


async def test_the_database_refuses_a_sentence_as_a_title_key(platform: Platform) -> None:
    """"A bilingual key, never a sentence" as something PostgreSQL enforces.

    The failure this prevents is a store that quietly fills `title_key` with "Your
    request was approved" — readable in exactly one language, and unreachable from
    the dictionaries the frontend renders from.
    """
    mine = await platform.account()

    with pytest.raises(IntegrityError):
        await platform.sql(
            "INSERT INTO notifications (id, recipient_employee_id, type, title_key, payload, "
            "entity_type, entity_id, dedupe_key) VALUES (:id, :recipient, 'approval.approved', "
            "'Your request was approved', '{}'::jsonb, 'leave_request', :entity, 'x')",
            {"id": uuid4(), "recipient": uid(mine), "entity": uuid4()},
        )


async def test_the_database_refuses_a_payload_that_is_not_an_object(
    platform: Platform,
) -> None:
    """Structured fields only: a payload that is a bare string is a sentence by
    another route."""
    mine = await platform.account()

    with pytest.raises(IntegrityError):
        await platform.sql(
            "INSERT INTO notifications (id, recipient_employee_id, type, title_key, payload, "
            "entity_type, entity_id, dedupe_key) VALUES (:id, :recipient, 'approval.approved', "
            "'notifications.approval.approved', '\"Your request was approved\"'::jsonb, "
            "'leave_request', :entity, 'x')",
            {"id": uuid4(), "recipient": uid(mine), "entity": uuid4()},
        )


async def test_one_delivery_row_per_channel_is_a_database_guarantee(
    platform: Platform,
) -> None:
    mine = await platform.account()
    raised = await raise_notification(platform, uid(mine))

    with pytest.raises(IntegrityError):
        await platform.sql(
            "INSERT INTO notification_deliveries (id, notification_id, channel, status) "
            "VALUES (:id, :notification, 'inapp', 'sent')",
            {"id": uuid4(), "notification": raised.notification_id},
        )


# --- the approval events ------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Cast:
    """A request route through a real department, and two holders of `hr`."""

    requester: Actor
    manager: Actor
    hr: Actor
    other_hr: Actor


@pytest.fixture
async def cast(platform: Platform) -> Cast:
    department = await platform.department("operaciones")
    position = await platform.position(department, "technician")

    requester = await platform.account()
    manager = await platform.account()
    # The per-assignment approver, which is what the route reads first.
    await platform.assign(
        requester.employee_id, department, position, manager_employee_id=manager.employee_id
    )
    return Cast(
        requester=requester,
        manager=manager,
        hr=await platform.account(roles=("hr",)),
        other_hr=await platform.account(roles=("hr",)),
    )


async def approve_both_levels(platform: Platform, cast: Cast) -> UUID:
    entity = uuid4()
    async with approval_flow(platform) as approvals:
        request_id = await approvals.submit(ENTITY, entity, uid(cast.requester))
        await approvals.decide(request_id, uid(cast.manager), DecisionKind.APPROVE)
        await approvals.decide(request_id, uid(cast.hr), DecisionKind.APPROVE)
    return request_id


async def test_submitting_tells_the_next_approver(platform: Platform, cast: Cast) -> None:
    async with approval_flow(platform) as approvals:
        await approvals.submit(ENTITY, uuid4(), uid(cast.requester))

    page = await inbox(cast.manager)

    assert page["total"] == 1
    item = page["items"][0]
    assert item["type"] == AWAITING
    # A key from the dictionary, never a sentence.
    assert item["title_key"] == "notifications.approval.awaiting_decision"
    assert " " not in item["title_key"]
    assert item["payload"]["level"] == 1
    assert item["payload"]["round"] == 1
    assert item["payload"]["requester_employee_id"] == cast.requester.employee_id
    assert item["entity_type"] == ENTITY
    assert item["read_at"] is None
    # Nobody else is told that somebody else's request was filed.
    assert (await inbox(cast.requester))["total"] == 0
    assert (await inbox(cast.hr))["total"] == 0


async def test_approving_at_level_one_hands_the_request_to_hr(
    platform: Platform, cast: Cast
) -> None:
    async with approval_flow(platform) as approvals:
        request_id = await approvals.submit(ENTITY, uuid4(), uid(cast.requester))
        await approvals.decide(request_id, uid(cast.manager), DecisionKind.APPROVE, "fine")

    for holder in (cast.hr, cast.other_hr):
        page = await inbox(holder)
        assert page["total"] == 1
        assert page["items"][0]["payload"]["level"] == 2
        assert page["items"][0]["payload"]["requester_employee_id"] == cast.requester.employee_id

    # The first approver keeps only the notification that asked them, and the
    # requester is not told about a level that decided nothing final.
    assert (await inbox(cast.manager))["total"] == 1
    assert (await inbox(cast.requester))["total"] == 0


async def test_the_second_approval_tells_the_requester(
    platform: Platform, cast: Cast
) -> None:
    request_id = await approve_both_levels(platform, cast)

    page = await inbox(cast.requester)

    assert page["total"] == 1
    item = page["items"][0]
    assert item["type"] == "approval.approved"
    assert item["title_key"] == "notifications.approval.approved"
    assert item["payload"]["outcome"] == "approved"
    assert item["payload"]["level"] == 2
    assert item["payload"]["approval_request_id"] == str(request_id)


async def test_a_rejection_tells_the_requester(platform: Platform, cast: Cast) -> None:
    async with approval_flow(platform) as approvals:
        request_id = await approvals.submit(ENTITY, uuid4(), uid(cast.requester))
        await approvals.decide(request_id, uid(cast.manager), DecisionKind.REJECT, "no")

    item = (await inbox(cast.requester))["items"][0]

    assert item["title_key"] == "notifications.approval.rejected"
    assert item["payload"]["outcome"] == "rejected"
    assert item["payload"]["level"] == 1
    # HR never had it, so HR is not told it was refused.
    assert (await inbox(cast.hr))["total"] == 0


async def test_a_return_tells_the_requester(platform: Platform, cast: Cast) -> None:
    async with approval_flow(platform) as approvals:
        request_id = await approvals.submit(ENTITY, uuid4(), uid(cast.requester))
        await approvals.decide(request_id, uid(cast.manager), DecisionKind.RETURN, "fix the dates")

    item = (await inbox(cast.requester))["items"][0]

    assert item["title_key"] == "notifications.approval.returned"
    # Not "draft": the request is back in draft, but what happened to it was a
    # return, and the client renders the outcome not the state.
    assert item["payload"]["outcome"] == "returned"


async def test_a_withdrawal_tells_the_requester(platform: Platform, cast: Cast) -> None:
    entity = uuid4()
    async with approval_flow(platform) as approvals:
        request_id = await approvals.submit(ENTITY, entity, uid(cast.requester))
        await approvals.withdraw(request_id, uid(cast.requester))

    item = (await inbox(cast.requester))["items"][0]

    assert item["title_key"] == "notifications.approval.withdrawn"
    assert item["payload"]["outcome"] == "withdrawn"
    # The approver is not told about a queue that emptied itself: there is nothing
    # for them to do.
    assert (await inbox(cast.manager))["total"] == 1


async def test_a_withdrawal_after_a_return_is_its_own_notification(
    platform: Platform, cast: Cast
) -> None:
    """Two events about one round, and both are news.

    A withdrawal taken while the request is back in draft shares the round with the
    return that put it there. If the withdrawal's dedupe key were derived from the
    state's status, it would collide with the return's and the requester would never
    learn that their own withdrawal had been recorded.
    """
    entity = uuid4()
    async with approval_flow(platform) as approvals:
        request_id = await approvals.submit(ENTITY, entity, uid(cast.requester))
        await approvals.decide(request_id, uid(cast.manager), DecisionKind.RETURN, "again")
        await approvals.withdraw(request_id, uid(cast.requester))

    page = await inbox(cast.requester)

    assert page["total"] == 2
    assert {item["title_key"] for item in page["items"]} == {
        "notifications.approval.returned",
        "notifications.approval.withdrawn",
    }


async def test_replaying_the_same_event_does_not_notify_twice(
    platform: Platform, cast: Cast
) -> None:
    """The retry a caller makes when a response was lost, at the event level.

    Both the state and the notification are asked for twice; the second ask is
    suppressed, and the suppression is on the record.
    """
    async with approval_flow(platform) as approvals:
        request_id = await approvals.submit(ENTITY, uuid4(), uid(cast.requester))
        state = await approvals.decide(request_id, uid(cast.manager), DecisionKind.APPROVE)

        replay = await approvals.decided(state)

    assert [outcome.duplicate for outcome in replay] == [True, True]
    assert (await inbox(cast.hr))["total"] == 1
    assert (await inbox(cast.other_hr))["total"] == 1
    assert (
        await count(
            platform,
            "audit_log",
            "action = 'notification.duplicate_suppressed'",
        )
        == 2
    )


async def test_the_whole_route_leaves_one_notification_per_person(
    platform: Platform, cast: Cast
) -> None:
    """Submitted, approved, approved: three events, one row each, no duplicates."""
    await approve_both_levels(platform, cast)

    assert (await inbox(cast.requester))["total"] == 1
    assert (await inbox(cast.manager))["total"] == 1
    assert (await inbox(cast.hr))["total"] == 1
    assert (await inbox(cast.other_hr))["total"] == 1
    assert await count(platform, "notifications") == 4
    assert await count(platform, "notification_deliveries") == 8
