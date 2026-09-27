"""Persistence contract for the anomaly scan, and the leave seam it reads through.

Two Protocols, and the second one is the ticket's named hole:

* **`AnomalyRepository` is the scan's storage.** Nothing commits: the pass writes
  its day and commits once, so a scan that fails half way through does not leave
  half a day's anomalies behind for the next run to reconcile. `insert` returns
  `None` when the row was already there, which is the idempotency the unique index
  states — reported rather than swallowed, so a second run can say "nothing to do"
  instead of looking like a run that found nothing wrong.
* **`LeaveLookup` is where ticket 25 plugs in.** "Was this person on approved leave
  on this date" is a *date* question and belongs to the leave module, which does not
  exist yet (ticket 25 is blocked by 24). The scan asks this and nothing else, so
  the day the leave tables arrive the change is one implementation and one line of
  wiring — not a condition threaded through the detection rules.
"""

from collections.abc import Sequence
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from app.domain.attendance.anomalies import Anomaly, NewAnomaly
from app.domain.attendance.models import AttendanceEvent


class AnomalyRepository(Protocol):
    async def employee_ids(self) -> list[UUID]:
        """Everybody whose working-time record is still open.

        The same population the month-end snapshot covers: a terminated record is
        history, and a night's pass that flagged it would be reporting on somebody
        who no longer owes the company anything.
        """
        ...

    async def events_for_day(
        self, employee_id: UUID, business_date: date
    ) -> list[AttendanceEvent]:
        """One day of the stream, corrections included, as the derivation reads it.

        The same query `AttendanceService` uses, deliberately: an anomaly judged
        from a different reading of the day than the one the day's own snapshot was
        built from would contradict the record it belongs to.
        """
        ...

    async def insert(self, anomaly: NewAnomaly, detected_at: datetime) -> Anomaly | None:
        """Record one anomaly, or `None` when this day already had it.

        The unique key is `(employee_id, business_date, type)`; the conflict is a
        second pass over a day it has already examined, which is not a mistake.
        """
        ...

    async def day_anomalies(
        self, employee_id: UUID, business_date: date
    ) -> list[Anomaly]:
        """Everything recorded against one person's one day, resolved ones included."""
        ...

    async def unnotified(self, business_date: date) -> list[Anomaly]:
        """The day's still-standing anomalies nobody has been told about yet.

        "Still standing" is `resolved_by_event_id IS NULL`: reminding somebody to
        make up a punch they have already made up is the one message this pass must
        not send.
        """
        ...

    async def mark_notified(self, anomaly_ids: Sequence[UUID], at: datetime) -> int:
        """Stamp the reminder on the rows it was raised for. Returns how many moved."""
        ...

    async def resolve(self, anomaly_ids: Sequence[UUID], event_id: UUID) -> list[Anomaly]:
        """Mark rows resolved by the event that cleared them (ticket 24's correction)."""
        ...

    async def commit(self) -> None: ...


class LeaveLookup(Protocol):
    """What the scan asks the leave module, and the only thing it asks."""

    async def is_on_leave(self, employee_id: UUID, business_date: date) -> bool:
        """Whether an approved leave covers this person's whole day.

        A *date*, not a range: the scan examines one day at a time, and a partial
        day's leave (an hour at the dentist) is not something this system records.
        """
        ...


class AssumeNoLeave:
    """Today's answer: nobody is on leave, because nothing records leave yet.

    Ticket 25 replaces this with the real lookup and changes one line of wiring. It
    is written as a class rather than a lambda so the substitution is a substitution
    and the docstring naming the ticket travels with the thing it describes.
    """

    async def is_on_leave(self, employee_id: UUID, business_date: date) -> bool:
        return False


__all__ = ["AnomalyRepository", "AssumeNoLeave", "LeaveLookup"]
