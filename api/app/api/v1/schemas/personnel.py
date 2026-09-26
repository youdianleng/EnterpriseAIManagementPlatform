"""Personnel change API schemas.

The request body is the change's structured detail and nothing else: a list of
`field`/`before`/`after` items whose field names are checked against the change
type by the domain, not here. `before` may be null (a field with no previous
value, and a caller who does not know it, state the same thing) but the key has to
be there, which is what makes "structured, not free text" a property of the shape
a client sends rather than of the validator's mood.

The response carries two status-shaped fields on purpose:

* `status` is what the document's own row says — `draft`, `pending`, `applied`,
  `cancelled`;
* `state` is the one computed field the UI reads, and it is the only one that can
  answer "approved but not yet in effect", because that is the engine's answer and
  not the row's.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.api.v1.schemas.base import StrictModel
from app.domain.approval.models import DecisionKind
from app.domain.personnel.models import ChangeState, ChangeType


class ApprovalDecisionRequest(StrictModel):
    """What an approver sends. Who may send it is the engine's answer."""

    decision: DecisionKind
    comment: str | None = Field(default=None, max_length=1000)


class FieldChangeIn(StrictModel):
    field: str = Field(min_length=1, max_length=60)
    before: Any | None = None
    after: Any


class PersonnelChangeCreate(StrictModel):
    """A draft. `employee_id` is absent for a join, which creates the employee."""

    change_type: ChangeType
    effective_date: date
    employee_id: UUID | None = None
    changes: list[FieldChangeIn] = Field(min_length=1)


class PersonnelChangeCancel(StrictModel):
    """Why it is being stopped. Required: a cancellation nobody can explain is the
    thing the record exists to prevent."""

    reason: str = Field(min_length=1, max_length=500)


class FieldChangeRead(BaseModel):
    field: str
    before: Any | None = None
    after: Any


class ApprovalDecisionRead(BaseModel):
    level: int
    round: int
    decision: str
    approver_employee_id: UUID
    comment: str | None = None
    decided_at: datetime


class ApprovalRead(BaseModel):
    """The engine's request, as far as a reader of this document needs it.

    Includes every round's decisions, because a request that was returned for
    correction and filed again is explained by the history, not by where it stands
    now.
    """

    request_id: UUID
    status: str
    round: int
    submitted_at: datetime | None = None
    decided_at: datetime | None = None
    decisions: list[ApprovalDecisionRead] = Field(default_factory=list)


class PersonnelChangeRead(BaseModel):
    id: UUID
    change_type: ChangeType
    employee_id: UUID | None = None
    effective_date: date
    state: ChangeState
    status: str
    changes: list[FieldChangeRead]
    applied_values: dict | None = None
    applied_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancelled_by_employee_id: UUID | None = None
    cancel_reason: str | None = None
    created_by_employee_id: UUID
    created_at: datetime
    updated_at: datetime


class PersonnelChangeDetail(PersonnelChangeRead):
    approval: ApprovalRead | None = None


class PersonnelChangePage(BaseModel):
    """A page of changes, plus what a caller needs to ask for the next one."""

    items: list[PersonnelChangeRead]
    total: int
    limit: int
    offset: int
