"""The draft's life: recorded when the assistant proposes it, expired when its time is up.

Two operations, and each one is a DESIGN requirement rather than a convenience.

**`record_draft` is §6.3's 「`agent_actions` 表全程留痕」, and it is the *platform's* write,
never the tool's.** The draft tools are pure (`app/ai/tools/draft.py`); they return a
`PrefillForm` and the node hands it here. This is the same split ticket 34 drew for answers:
the graph relays, and a domain service owns the row. What travels into the row is the tool's
name (a registry key), the tool's input and output as structured values, and the form.

**`latest_draft` is §6.3's 「过期后 `status=expired`」, and it *records* what it observes.**
The comparison happens in SQL against the database's clock (`repository.py`), and when it
says the draft has lapsed and the row still reads `proposed`, this method writes `expired`
before answering. A reader that only *derived* the status would leave the audit table saying
a stale draft was still waiting for somebody who is never coming back to it — and §6.3 asks
for the status, not for a filter.

**A conversation is ensured, not required.** A draft belongs to a conversation (§3.6's
`conversation_id`), and the draft branch can be reached by a question that has not created
one yet — the answer path is what normally mints it. So the row is written against the
caller's named conversation when there is one, and against a new conversation titled from
the question when there is not: the same `ensure_conversation` the answer path uses, so
"whose conversation is this" has one implementation. Ticket 41's pause/resume hands the
graph the conversation it was given, which is why the branch also *returns* the id it used.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access.principal import Principal
from app.domain.agent.models import AgentAction, DraftStatus, PrefillForm
from app.domain.agent.repository import PostgresAgentActionRepository
from app.domain.answer.repository import PostgresAnswerRepository
from app.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RecordedDraft:
    """One recorded draft: the row, and the conversation it was filed under.

    `conversation_id` travels separately from `action.conversation_id` because it is the
    one the *caller* should carry forward — the same value when the caller named one, and
    the id this call minted when it did not. A node that recorded a draft into a new
    conversation and then forgot the id would resume into a second one.
    """

    action: AgentAction
    conversation_id: UUID

    @property
    def status(self) -> DraftStatus:
        return self.action.status


class AgentActionService:
    """§3.6's `agent_actions`, as the platform writes and reads it."""

    def __init__(
        self,
        repository: PostgresAgentActionRepository,
        answers: PostgresAnswerRepository,
        session: AsyncSession,
        *,
        ttl_hours: int,
    ) -> None:
        self._repository = repository
        self._answers = answers
        self._session = session
        self._ttl_hours = ttl_hours

    @property
    def ttl_hours(self) -> int:
        """How long a draft stands. §6.3's 「默认 24h」, as configured."""
        return self._ttl_hours

    @property
    def repository(self) -> PostgresAgentActionRepository:
        """The repository this service writes through (ticket 41).

        Exposed so the confirmation path runs its statements in **this service's
        transaction** rather than opening a second one: the row lock
        `PostgresAgentActionRepository.load_for_update` takes has to cover the entity
        write that follows it, and a separate repository object on a separate session
        would hold a lock nothing else could see. Reading is all a caller gets — the
        service's own operations remain the only write surface it advertises.
        """
        return self._repository

    async def record_draft(
        self,
        *,
        principal: Principal,
        conversation_id: UUID | None,
        question: str,
        tool_name: str,
        tool_input: Mapping[str, Any],
        tool_output: Mapping[str, Any],
        form: PrefillForm | None,
        thread_id: str | None = None,
    ) -> RecordedDraft:
        """Write one proposed draft and commit it, with the caller's own identity.

        `principal.user_id` is the row's owner — not a tool argument and not a state field:
        the only thing that says whose draft this is, is the principal the request resolved.
        """
        filed_under = await self._answers.ensure_conversation(
            user_id=principal.user_id, question=question, conversation_id=conversation_id
        )
        action = await self._repository.insert(
            conversation_id=filed_under,
            user_id=principal.user_id,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=tool_output,
            form=form,
            ttl_hours=self._ttl_hours,
            thread_id=thread_id,
        )
        # One commit, after the row has come back whole: see `repository.py` for why a read
        # after a commit would find nothing under the request's permission context.
        await self._repository.commit()
        logger.info(
            "agent_draft_recorded",
            tool_name=tool_name,
            ttl_hours=self._ttl_hours,
            has_form=form is not None,
        )
        return RecordedDraft(action=action, conversation_id=filed_under)

    async def latest_draft(
        self, *, user_id: UUID, conversation_id: UUID
    ) -> AgentAction | None:
        """The conversation's newest draft, with a lapsed one recorded as expired.

        `user_id` is passed rather than inferred from the conversation on purpose: the
        caller's own user id is what the read is scoped by, exactly as the conversation read
        is (`PostgresAnswerRepository.load_for`), so an id from elsewhere is not reachable
        through this method however it was obtained.
        """
        owned = await self._answers.owns(user_id, conversation_id)
        if not owned:
            # The same answer as "no such conversation": telling them apart would make this
            # a method that reports which conversation ids exist (ticket 37's rule).
            return None
        stored = await self._repository.newest_for_conversation(conversation_id)
        if stored is None:
            return None
        if stored.expired and stored.action.status is DraftStatus.PROPOSED:
            moved = await self._repository.mark_expired(stored.action.id)
            await self._repository.commit()
            logger.info("agent_draft_expired", moved=moved, tool_name=stored.action.tool_name)
            return replace(stored.action, status=DraftStatus.EXPIRED)
        return replace(stored.action, status=stored.status)


def service_for(session: AsyncSession, *, ttl_hours: int) -> AgentActionService:
    """The service on one session, wired the way every caller wires it.

    A function rather than a constructor call at each site for the reason
    `app/ai/tools/services.py` gives about its own wiring: the repository, the answer
    repository and the session are one arrangement, and a second site that assembled them
    differently would be a second answer to "what is a draft".
    """
    return AgentActionService(
        PostgresAgentActionRepository(session),
        PostgresAnswerRepository(session),
        session,
        ttl_hours=ttl_hours,
    )


__all__ = ["AgentActionService", "RecordedDraft", "service_for"]
