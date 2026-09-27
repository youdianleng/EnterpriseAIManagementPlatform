"""Persistence contract for the correction document.

Three things about this interface are load-bearing:

* **Nothing here writes to the stream.** The event a correction produces is
  appended by `AttendanceRepository.append_event`, the same method a punch goes
  through, so the append-only guarantee has one implementation: there is no
  "replace" to call and no update to forget not to call.
* **`punch_lineage` returns rows, not a punch.** The flow has three questions about
  a day and a kind — does the punch exist, which row does the chain end at, and did
  the document's own pair identify exactly one punch — and all three are answered
  by the lineage: every row of that punch's correction chain, oldest first. A
  method that returned "the punch" would have to decide the chain question in SQL,
  where the derivation's rule does not live.
* **Nothing commits except `commit`.** The service commits once per operation, so a
  document's status and the event it applied land together or not at all — the way
  a punch and the day's snapshot do.
"""

from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from app.domain.approval.models import ApprovalStatus
from app.domain.attendance.corrections import (
    Correction,
    CorrectionInput,
    CorrectionPatch,
    CorrectionQuery,
    CorrectionState,
)
from app.domain.attendance.models import AttendanceEvent, EventType


class CorrectionRepository(Protocol):
    # --- the document -------------------------------------------------------

    async def save_correction(self, correction: CorrectionInput) -> Correction:
        """Write a draft, and return it as stored."""
        ...

    async def get_correction(self, correction_id: UUID) -> Correction | None:
        """One document, by id."""
        ...

    async def list_corrections(
        self, query: CorrectionQuery
    ) -> list[tuple[Correction, CorrectionState]]:
        """A page of documents, newest first, with the state a reader sees.

        The state is computed in the query — one `outerjoin` to the engine's
        requests and a `CASE` — so a page of fifty corrections does not cost fifty
        round trips. `corrections.state_of_correction` states the same rule in
        Python, and the tests run both over one corpus of rows, because two
        expressions of one rule are exactly the pair that drifts.
        """
        ...

    async def count_corrections(self, query: CorrectionQuery) -> int:
        """How many documents match, for the page the caller is looking at."""
        ...

    async def approval_status_of(self, correction_id: UUID) -> ApprovalStatus | None:
        """The engine's status for this document's request, if it has one."""
        ...

    # --- writes ------------------------------------------------------------

    async def write_draft(
        self, correction_id: UUID, *, patch: CorrectionPatch
    ) -> Correction:
        """Change a draft. Only a draft: the caller has already checked."""
        ...

    async def mark_submitted(
        self, correction_id: UUID, *, request_id: UUID, at: datetime
    ) -> Correction:
        """Record that the document is with the engine."""
        ...

    async def mark_applied(
        self, correction_id: UUID, *, event_id: UUID, at: datetime
    ) -> Correction:
        """Record that the append happened, and which row it wrote."""
        ...

    async def lock_next_unapplied(
        self,
        *,
        exclude: frozenset[UUID] = frozenset(),
        only: UUID | None = None,
    ) -> Correction | None:
        """The oldest filed document that is not applied yet, locked for update.

        `FOR UPDATE ... SKIP LOCKED` for the reason the personnel applier gives: two
        workers take two documents rather than both applying the same one, and the
        lock is released by the commit that ends it. A document whose request is
        not approved is returned all the same — the service asks the engine, which
        is the only thing that can answer — and is then left alone, which is why
        the queue is selected by `applied_at` rather than by a status copied from
        the engine.
        """
        ...

    # --- the stream --------------------------------------------------------

    async def punch_lineage(
        self, employee_id: UUID, business_date: date, kind: EventType
    ) -> list[AttendanceEvent]:
        """Every row of one punch's correction chain, oldest first.

        The punch first, then the corrections that restate it, in the order they
        were appended — which is the order the screen shows and the order the
        derivation resolves. A punch that was never made has no lineage at all,
        which is how the flow tells "restate this" from "make this up".
        """
        ...

    async def commit(self) -> None: ...
